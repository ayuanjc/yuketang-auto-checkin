# 协议事实表：www.yuketang.cn（v3.1）

> 来源：2026-11 对 9 个开源实现的源码审计（thuhollow2/yuketang、dvdsanyi/Yuketang-Helper-Web、TrickyDeath/RainClassroomAssitant、ZhengXieGang/YuKeTang-CheckIn-Helper 等）。
> 状态标记：✅ 已由多份实现交叉验证 ｜ ⚠️ 单一来源 ｜ ❓ 待真机抓包确认

## 1. 传输约定

| 项 | 值 | 状态 |
|---|---|---|
| 域名族 | `www` / `pro`(荷花) / `changjiang`(长江) / `huanghe` | ✅ |
| 必带头 | `xtbz: ykt`、`desktop-v: v2`、`X-Client: desktop`、`Origin: file://` | ✅ |
| 鉴权 | cookie（含 `csrftoken`/`sessionid`）；`checkin` 成功后响应头 `Set-Auth` → 后续 `Authorization: Bearer <token>` | ✅ |
| 会话有效期 | 约 2 周，过期需重扫 | ✅ |
| 实时通道 | `wss://{domain}/wsapp/` | ✅ |

## 2. 登录（扫码）

```
客户端 → wss: {"op":"requestlogin","role":"web","version":1.4,"type":"qrcode","from":"web"}
服务端 → {"ticket":"https://.../qr?c=<ticket>"}
用户扫码
服务端 → {"UserID":"...","Auth":"..."}    // Auth 换 cookie
```
HTTP 备选：`/api/v3/user/login/pre-info`、`/wait-for-scan`、`/login`、`/login-with-code`。⚠️

## 3. 端点表

| 方法 | 路径 | 关键字段 | 状态 |
|---|---|---|---|
| GET | `/api/v3/user/basic-info` | `code==0` 即会话有效 | ✅ |
| GET | `/api/v3/classroom/on-lesson-upcoming-exam` | `data.onLessonClassrooms[].{lessonId,classroomId,classroomName,startTime}` | ✅ |
| GET | `/api/v3/classroom/on-lesson` | 同上，更细 | ⚠️ |
| GET | `/api/v3/connection/get-token` | wss 连接 token | ⚠️ |
| POST | `/api/v3/lesson/checkin` | `{source:5\|14\|21, lessonId}` → `data.lessonToken/identityId` | ✅ |
| POST | `/api/v3/lesson/checkin` | `{source:14, lessonId, ticket, t, s}` 动态码回放 | ✅ |
| POST | `/api/v3/lesson/notkn/checkin` | `{source:14, inviteCode:"12345"}` 5 位暗号 | ✅ |
| GET | `/api/v3/lesson/fetch-dynamic-invitation` | `?lessonId=&v=2`，若对学生开放即免拍照代扫 | ❓ 优先级最高 |
| GET | `/api/v3/lesson/get-invitation` | 当前签到邀请（二维码内容） | ⚠️ |
| GET | `/api/v3/lesson/checkin-list` / `uncheckin-list` | 到课名单，用于自检是否已签 | ✅ |
| GET | `/api/v3/lesson/checkin/detail` | 本人签到明细 | ⚠️ |
| GET | `/api/v3/lesson/presentation/fetch` | `?presentation_id=` → `data.slides[].problem` | ✅ |
| POST | `/api/v3/lesson/problem/answer` | `{problemId, answer:[]}` | ✅ |
| POST | `/api/v3/lesson/danmu/send` | 弹幕（非目标，留作活跃度伪装） | ✅ |
| POST | `/api/v3/lesson/redenvelope/prepare` | 红包（同上） | ⚠️ |
| POST | `/v/course_meta/join_classroom` | `{id:"6位班级邀请码"}` | ⚠️ |

## 4. `msg` 取值分支

| msg | 含义 | 策略链动作 |
|---|---|---|
| `OK` | 签到成功 | 终止，计次 |
| `LESSON_END` / `LESSON_END_JOIN` | 课堂已结束 | **立即终止整条链**，标记 skipped |
| `LESSON_INVITE_CODE_TIMEOUT` | 暗号过期 | 回退到动态码/位置 |
| 其他 | 未知 | 记录原值，重试 2 次后回退 |

## 5. wss 帧

```
客户端 → {"op":"hello","userid":<identityId>,"role":"student","auth":<lessonToken>,"lessonid":<id>}

服务端 → op 取值：
  hello             建连确认
  fetchtimeline     timeline[].{pres,si}  回放已讲页
  showpresentation  切到某 PPT 页
  slide             翻页
  unlockproblem     unlockedproblem:[]    解锁小测 → 触发答题
  lessonfinished    下课 → 关闭连接并结算
```
**每帧带 `dt`（服务端毫秒时间戳）→ 必须喂给 `obs.Clock.SyncFromServerMillis` 校时**，答题 deadline 判断依赖它。

## 6. 已知坑

1. 2024 年后 `presentation/fetch` **不再下发正确答案** → 必须题库/共享池/LLM，任何"前端取答案"的实现已失效（zhangchi2004/THU-Yuketang-Helper 已注明失效）。
2. 答案下发到 `unlockproblem` 帧与 `fetch` 接口之间存在时间差，先等帧再拉接口，否则拿到空 problem。
3. `source` 取值在不同域名族可能不同，必须走 `internal/ykt/profile`，禁止硬编码。
4. 会话过期不会在任意接口报错，**只有 `basic-info` 的 `code` 可信**，保活只认它。
5. 位置签到：9 个开源实现**零实现**，`latitude/longitude/sign_type` 从未出现 → 字段名未知，必须抓包（见下）。

## 7. 位置签到的三条取证路线（按成本排序）

1. `GET /api/v3/lesson/fetch-dynamic-invitation` 实测：若返回带坐标或 `sign_type`，直接拿到协议形状（免抓包）。
2. Android 真机 + mitmproxy + frida 绕 SSL pinning；APK 样本 `yktmobile-0.0.2-debug.apk`（来自 ZhengXieGang/YuKeTang-CheckIn-Helper 仓库）。
3. 微信小程序 `wxapkg` 解包 → 搜 `wx.request` 的 checkin 调用，看 body 组装。

结论写入本文件前必须先有一条真实抓包/实测记录，禁止凭猜测定稿。

## 8. 实测记录（2026-11-16，账号 id=102537495，域名 www.yuketang.cn）

### 8.1 登录握手：已打通

| 步骤 | 请求 | 观察 |
|---|---|---|
| 1 | `GET /api/v3/user/login/pre-info`（无 body） | `{token, qrContent, qrImage}`；`qrContent` = `http://weixin.qq.com/q/...`，`qrImage` = `https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=...` |
| 2 | `POST /api/v3/user/login/wait-for-scan` body **恰好** `{"token":T}` | **长轮询**，连接挂起 30s+ 不返回；多加任一键（type/role/version）→ `400 {"code":50400,"msg":"BAD_REQUEST"}`；两个 poller 并发会互相踩 → 必须单实例 |
| 3 | 扫码后 | 服务端下发 `data.token != T`（verify token），微信同时推 **4 位数字** 到用户手机 |
| 4 | `POST /api/v3/user/login/login-with-code {"token":<verify token>,"code":"6001"}` | `{"code":0,"data":{"id":"102537495",...}}` 并 Set-Cookie 落 `sessionid` + `sid` |

判定成功的唯一依据是 **sessionid cookie**，不是 `code:0`——中间态同样回 200 + code:0。
`POST /api/v3/user/login {"token":T}` 也是长轮询，可作兜底通道。
二维码有效期 < 5 min，过期表现为 `code in (500, 50000, 50001)`，需重新 `pre-info`。

### 8.2 账密登录：此路不通

- `GET /pc/register/get_pws_public_key/` → PEM 公钥，正常。
- `POST /pc/login/verify_pwd_login/` body `{name, pwd: base64(RSA_PKCS1v1_5(pwd)), type:"PP", ticket, randstr, hcaptcha_token}` → `{"msg":"腾讯防水墙校验失败","status_code":300416,"success":false}`。`ticket/randstr` 来自腾讯 TCaptcha，服务端纯批量账密登录被防水墙锁死。
- `POST /api/v3/user/login` 直接塞账密 → `50400 BAD_REQUEST`（端点不吃这种形状）。

### 8.3 会话与 lesson 作用域

- `GET /api/v3/user/basic-info` → `{id,name,nickname,school:"",role:0,dateJoined,binding}`，是唯一可信的存活探针。
- `GET /v2/api/web/courses/list?identity=2` → `{errcode:0,data:{list:[]}}`；未加课账号为空，有课账号的字段形状仍待确认。
- `GET /api/v3/classroom/on-lesson-upcoming-exam` → `{code:0,data:{onLessonClassrooms:[],upcomingExam:[]}}`。
- `GET /api/v3/lesson/fetch-dynamic-invitation?lessonId=N&v=2` 在**没有 lesson 作用域**时 → **HTTP 401**（`code:0, msg:""`）。该端点要求 `Authorization: Bearer <lessonToken>`，而 lessonToken 只能由一次成功的 `/api/v3/lesson/checkin` 通过 `Set-Auth` 换到。
  **推论：动态码回放不是免签到入口，绕不开先成功 enter 签到**（source=5 必须过），这也解释了为什么所有开源实现都先打 enter。

### 8.4 对产品的直接结论

1. 绑定向导必须两步：扫码 + 用户报 4 位码。全自动绑定要么接腾讯 captcha，要么走私钥/短信通道，成本由产品承担。
2. session 按 2 周设计：到期前 48h 提醒续绑，续绑同样是两步；续绑失败要有降级（短信提醒 + 人工）。
3. 台账必须信封加密 `sessionid` 与 `sid` 两枚 cookie，账号主键用 basic-info 的 `id`。
4. 位置签到字段仍未知；没有进行中的课就无法验证 checkin 全链，字段还得靠真机抓包。

## 9. 课节列表与签到语义（2026-11-16 实测，全部为 200 响应）

### 9.1 课节列表（此前一直没找到的真端点）

```
GET /v2/api/web/logs/learn/{classroom_id}?actype=-1&page=0&offset=20&sort=-1
→ {"errcode":0,"errmsg":"Success","data":{
     "activities":[{"type":14,"id":35130460,"courseware_id":"1821000000000000009",
                    "title":"10月25日授课","create_time":1792894878000,
                    "attend_status":false,"is_finished":true}, ...],
     "prev_id":34884518,"has_more":false}}
```

该端点走 **v2 web 信封** `{errcode,errmsg,data}`，不是 v3 的 `{code,msg,data}`；client 已同时兼容两者。
`errcode` 非 0 时业务失败，HTTP 仍是 200。`actype=14` 只看课堂，`-1` 看全部（含课件/考试）。

### 9.2 lessonId = activities[].courseware_id（关键语义）

checkin 的 `lessonId` 取 **`courseware_id`（19 位雪花号）**，不是 `activities[].id`：

| body | 结果 |
| --- | --- |
| `{"source":5,"lessonId":"1821000000000000009"}` | 200 `{"code":50004,"msg":"LESSON_END"}`（端点通，课已结束） |
| `{"source":5,"lessonId":"35130460"}`（活动 id） | 200 `{"code":50016,"msg":"CLASSROOM_NOT_FOUND"}` |
| `{"source":5,"lessonId":1821000000000000009}`（数字） | **400** `{"code":50400,"msg":"BAD_REQUEST"}` |
| `{"source":5}` / `{"lesson_id":...}` | 200 `{"code":50002,"msg":"LESSON_UNAUTHORIZED"}` |

推论：lessonId 必须以**字符串**序列化；后端校验顺序是「lessonId 是否存在 → 解析 classroom → 课是否结束」。
`LESSON_END` 响应**不带** `Set-Auth`，即 lessonToken 只能由一次**成功**签到换取。

### 9.3 web 端的签到只有一个入口：暗号

前端 bundle（`pc.js`, 1.2.354）里唯一的签到调用：

```js
checkin: function (p, f) {
  var m = API.pc.index.new_check_in;           // /api/v3/lesson/notkn/checkin
  request.post(m, {source: p, inviteCode: f})  // 只有 source + inviteCode
}
```

```
POST /api/v3/lesson/notkn/checkin {"source":14,"inviteCode":"00000"}
→ 200 {"code":50023,"msg":"LESSON_INVITE_CODE_TIMEOUT"}   // 无进行中课堂时的回答
```

结论：**暗号签到不需要 lessonId**；`/api/v3/lesson/checkin`（带 lessonId/ticket）是 APP 的路径，
web 端的"扫码签到"在网页里根本没有实现——这正是"在场者拍照 + 服务端回放 ticket"能成立的原因。

### 9.4 其它已确认端点

| 端点 | 用途 |
| --- | --- |
| `GET /v2/api/web/courses/list?identity=2` | 全部课程（52 门含历史学期） |
| `GET /v2/api/web/classrooms/{cid}?role=5` | 单课详情（course_name/teacher_name/university_domain/term） |
| `GET /api/v3/lesson-summary/student` | 学习总结 |
| `GET /api/v3/lesson-summary/student/presentation` | 课堂 PPT |
| `POST /api/v3/lesson-summary/tag` | 幻灯片打点 |
| `GET /api/v3/classroom/on-lesson-upcoming-exam` | 进行中的课 + 待考（调度触发源） |
| `POST /api/v3/lesson/checkin` | 带 lessonId（+ticket/t/s）的签到 |
| `POST /api/v3/lesson/notkn/checkin` | 暗号签到 |
| `GET /api/legacy/lesson/get-token` | legacy，参数名 PascalCase `UserID`，不认 sessionid → 弃用 |

### 9.5 抓包方法（可复用）

网页抓包不需要代理：ego 浏览器 + CDP 注入 cookie 即可。

```js
await cdp("Network.enable", {});
await cdp("Network.setCookie", {name:"sessionid", value:SID, domain:"changjiang.yuketang.cn", path:"/", secure:true});
await cdp("Network.setCookie", {name:"sid", value:SID2, ...});
await page.goto("https://changjiang.yuketang.cn/v2/web/studentLog/26000011");
await page.evaluate(() => performance.getEntriesByType("resource").map(e => e.name));
```

`?sessionid=` 查询参数**无效**（会跳登录页），只有 cookie 生效。学生页实际只发 6 个 XHR，
其中就包含 9.1 与 9.4 的端点。
