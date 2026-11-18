# 雨课堂自动签到（服务端）

把「到点掏手机扫码」换成服务端按课表发一次 HTTP 请求。

能不能算上，不取决于我们发得多快，而取决于上游那一步校验要不要证据。本仓库就是围绕这件事的完整实现：协议客户端、账号与凭据管理、课节发现、签到入口、考勤回读、部署与运维模板。

---

## 一、这个仓库是什么

- **协议层实现**：登录态、请求头、课节列表、签到提交、考勤回读，全部只用 Python 3.9+ 标准库。
- **单账号自动化核心**：一个账号、一台设备、按课表轮询；不含任何多租户与代他人提交的逻辑。
- **部署模板**：systemd 单元、nginx 站点片段与限流、fail2ban 规则、构建与完整性校验脚本。
- **工程文档**：协议抓包结论、架构、数据模型、接口约定、部署运维、安全与完整性设计。

## 二、这个仓库不是什么

- 不含任何前端界面（Web UI、App、小程序都不含）。
- 不含业务层：下单、钱包、邀请码、代他人提交、账号租借相关代码全部不在本仓库。
- 不修改官方客户端，不伪造登录态，不构造不存在的签到凭据。
- 不针对任何具体学校或具体课程；示例里出现的域名、课程号、课节号都是占位值。

## 三、目录结构

    .
    ├── src/
    │   ├── server.py        协议客户端：请求头、课节发现、签到、考勤回读
    │   ├── svc.py           核心服务端（只监听回环地址，token 鉴权，不服务页面）
    │   ├── svcclient.py     调用核心服务端的瘦客户端
    │   ├── acct.py          账号入库（SQLite）+ 一用户一账号约束
    │   ├── secure.py        凭据落盘加密 + 进程完整性校验
    │   ├── loc.py           位置签到链路（预览 → 提交）
    │   ├── lesson_watch.py  课中长连接：随堂小测与点名的旁路观察
    │   ├── appsession.py    移动端请求头的字段级对齐
    │   ├── ai.py            随堂小测代答的可选 LLM 通道
    │   ├── alert.py         失败告警 webhook（企微/钉钉/飞书/Bark 通用）
    │   └── precheck.py      开课前的自动体检
    ├── deploy/
    │   ├── build_secure.sh        源码 → 字节码，并生成完整性清单
    │   ├── systemd/               服务单元与定时器
    │   ├── nginx/                 站点片段、CSP、限流、静态缓存
    │   └── fail2ban/              登录与 404 扫描的封禁规则
    ├── tools/window_watch.py      签到入口的响应码时间序列采样器
    └── docs/                      协议与工程文档（见第九节）

## 四、快速开始

    # 1) 环境：Python 3.9+，无第三方依赖
    python3 -V

    # 2) 约定一个工作目录（下称 YKT_HOME），凭据与数据都放这里
    export YKT_HOME=/opt/yktsign
    mkdir -p $YKT_HOME/accounts $YKT_HOME/data

    # 3) 写入一份登录态：从浏览器开发者工具里复制 Cookie
    cat > $YKT_HOME/cookies.json <<'JSON'
    {"sessionid": "<你的 sessionid>", "csrftoken": "<你的 csrftoken>", "sid": "<你的 sid>"}
    JSON
    chmod 600 $YKT_HOME/cookies.json

    # 4) 冒烟：能读到自己的资料就说明登录态有效
    cd src && python3 -c "import server as S; print(S.acct_info('main'))"

    # 5) 看当前有没有正在上课的课节
    python3 -c "import server as S; print(S.live_now('main'))"

    # 6) 对某一节提交签到（lessonId 来自上一步）
    python3 -c "import server as S; print(S.do_checkin('main', '1821000000000000001'))"

    # 7) 回读考勤：确认老师那张表里到底算没算上
    python3 -c "import server as S; print(S.attend_state('main', '26000002', '1821000000000000001'))"

第 7 步是整个项目的判断依据：**提交成功不等于考勤计入**。签到接口返回 code 0 只说明请求被接受，必须回读学习记录里的 attend_status 才能确认。

## 五、核心链路

    登录态(cookie) ──► 课节发现 ──► 签到提交 ──► 考勤回读 ──► 记录与通知
         │                │              │              │
      secure 加密      logs/learn     checkin 接口   attend_status

1. **登录态**：桌面端只需要 xtbz + Referer 两个头；移动端专属端点（扫码解析）另需 x-client/app 与 deviceCode。两套头不能混用，混用会被 401 打回。
2. **课节发现**：没有「课表」接口，只能从每个课堂的历史学习记录里反推——把每节课的 create_time 归到 (周几, 时:分)，同一 (学科, 周几, 时刻) 命中多次即视为固定课。详见 src/server.py 的 timetable()。
3. **签到提交**：POST 签到接口，body 只有 lessonId 与 source 两个字段。source 是入口编号，不同客户端走不同值。
4. **考勤回读**：GET 学习记录，按 courseware_id 找本节课，读 attend_status 与 is_finished。
5. **记录与通知**：成功/失败/未计入三种状态分别落库，并通过站内消息或 webhook 告知。

## 六、关键实现点

| 主题 | 做法 | 位置 |
| --- | --- | --- |
| 连接复用 | 自建 keep-alive 连接池，避免每请求新建 TLS 握手 | src/server.py `_http_req` |
| 半开连接 | 记录连接存活时间，超时前主动丢弃，防止复用到已断的套接字 | src/server.py `_ka_drop` |
| 登录态失效判定 | code 50000 / 401 都视为会话失效，立即停止该账号的轮询并告警 | src/server.py、src/precheck.py |
| 课表反推 | 从历史课节聚类出「周几 + 时刻」，命中次数作为可信度 | src/server.py `timetable` |
| 并发控制 | 课表预热最多 8 个并发，避免对上游产生过大压力 | src/server.py `timetable` |
| 凭据落盘 | AES 信封加密 + 文件权限 600，密钥不进仓库 | src/secure.py |
| 响应码采样 | 对签到入口按秒级/分钟级双档采样，画出响应码时间序列 | tools/window_watch.py |
| 位置签到 | 先预览拿到范围判定，再提交坐标；不盲提交 | src/loc.py |

## 七、技术边界（实测结论）

这些结论来自对公开 Web/移动端接口的抓包与重放，只描述**协议行为**，不构成对任何服务的绕过手段：

- 普通签到：提交 lessonId + source 即可，服务端不再要求扫码证据。
- 动态二维码签到：二维码里的凭据由服务端密钥签发，客户端没有签名逻辑；窗口期内服务端拒绝一切无凭据提交，返回 50070。要过这一关，必须有一个人在教室真扫一次。
- 非本班正式成员：无论窗口是否开启都会被同一条错误码拒绝，这条错误码因此不能单独用来判断「窗口是否开启」。
- 位置签到：坐标由客户端提交，但服务端会先用一个「签到资格」接口发令牌；令牌拿不到时坐标根本发不出去。
- 口头暗号、手势、随机点名应答：没有可提交的接口，属于协议层无法覆盖的部分。

结论写成一句话：**能被自动化的是「提交一个请求」，不能被自动化的是「证明你人在教室」。** 本仓库只做前者。

## 八、部署

    # 1) 源码就位
    git clone <repo> /opt/yktsign-src && cd /opt/yktsign-src

    # 2) 编译成字节码并生成完整性清单
    sudo bash deploy/build_secure.sh

    # 3) 服务单元
    sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ykt-svc ykt-alarm.timer ykt-window-watch

    # 4) 反向代理与限流
    sudo cp deploy/nginx/*.conf /etc/nginx/snippets/
    # 在站点配置里 include 需要的片段，然后：
    sudo nginx -t && sudo systemctl reload nginx

    # 5) 登录暴力破解防护
    sudo cp deploy/fail2ban/*.conf /etc/fail2ban/jail.d/
    sudo systemctl restart fail2ban

要点：

- 服务只监听 127.0.0.1，由反向代理对外；任何对外端口都不要直连业务进程。
- 启动时校验字节码清单，源码被改动会拒绝启动（见 src/secure.py）。因此改代码必须走 build_secure.sh，不能手工覆盖 .pyc。
- nginx 备份文件不要放在 sites-enabled/ 下，会被一并加载导致配置冲突。

## 九、文档

| 文件 | 内容 |
| --- | --- |
| docs/www.yuketang.cn.md | Web 端接口清单与请求头要求 |
| docs/06-动态码与位置签到.md | 动态码凭据结构与位置签到两步链路 |
| docs/01-架构设计.md | 分层、数据流、模块职责 |
| docs/02-开发规范.md | 代码风格、命名、错误码约定 |
| docs/03-数据模型.md | 表结构与字段语义 |
| docs/04-接口约定.md | 内部接口与错误码 |
| docs/05-部署运维.md | 部署、备份、日志、巡检 |
| docs/07-安全与完整性.md | 凭据加密、完整性校验、登录防护 |
| docs/11-会话失效与请求头.md | 两套请求头的差异与失效判定 |
| docs/12-现场验证手册.md | 现场怎么验、验什么、怎么记录 |
| docs/16-按课程签到.md | 按课程名而非课堂号定位课节 |
| docs/campus-geo.md | 校园坐标系与偏移换算 |

## 十、许可

AGPL-3.0，见 LICENSE。

分发或部署为网络服务时，按 AGPL-3.0 第 13 条需要向使用者提供对应源码。
