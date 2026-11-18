"""雨课堂签到回放服务：摄像头扫码 -> 服务端回放 -> 返回 lessonToken。仅标准库。"""
import base64
import hashlib
import hmac
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import secure
DOMAIN = "changjiang.yuketang.cn"


def safe_domain(dm):
    """出站域名白名单：只认 *.yuketang.cn。

    cookie 里的 __domain 来自用户提交的绑定表单，若不校验就能把请求打到
    127.0.0.1 / 169.254.169.254 这类地址（SSRF）。非法值一律回落到默认域名。
    """
    dm = str(dm or "").strip().lower().rstrip(".")
    if not dm or not re.match(r"^[a-z0-9][a-z0-9.-]*$", dm):
        return DOMAIN
    if dm == "yuketang.cn" or dm.endswith(".yuketang.cn"):
        return dm
    return DOMAIN
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
# 设备标识：本仓库面向「一台设备、一个本人账号」的个人自动化场景，
# 因此使用固定的设备指纹，不做多账号之间的指纹分散。
DEVICE_CODE = hashlib.md5(b"yktsign-device").hexdigest()
APP_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 yuketang/1.3.6")
LOG = os.path.join(BASE, "events.jsonl")
CTYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
lock = threading.Lock()


CIDS = ["26000002", "26000012"]
CIDS_BY_ACCT = {
    "main": CIDS,
    "friend": ["26000013", "26000014", "26000015"],
}


def _keys():
    """key.txt 支持多行：一行一把钥匙（自己一把、朋友一把，可单独吊销）。"""
    try:
        with open(os.path.join(BASE, "key.txt"), encoding="utf-8") as f:
            return {x.strip() for x in f if x.strip()}
    except OSError:
        return set()


def lesson_of(raw):
    u = urllib.parse.urlparse(raw)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    return str(q.get("lessonId") or q.get("lesson_id") or q.get("id") or "")


def cached_token(lid):
    """同一节课已经拿到过 token 就直接回放，避免重复打卡留下多条轨迹。"""
    if not lid:
        return ""
    try:
        with open(os.path.join(BASE, "lesson_auth.json"), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return ""
    if str(d.get("lessonId") or "") == str(lid):
        return d.get("token") or d.get("auth") or ""
    return ""


SCAN_CACHE = {}


def scan_all(force=False, acct="main"):
    """全课扫描：拿课程列表 -> 并发查每门课最近课节 -> 挑出 is_finished=False 的正在上课的课。"""
    now = time.time()
    hit = SCAN_CACHE.get(acct)
    if not force and hit and now - hit["at"] < 120:
        return hit["data"]
    _, text, _ = ykt("GET", "/v2/api/web/courses/list?identity=2", timeout=15, acct=acct)
    cids = []
    try:
        d = json.loads(text) or {}
        for c in (d.get("data") or {}).get("list") or []:
            cid = c.get("classroom_id") or c.get("id")
            if cid:
                cids.append((str(cid), c.get("name") or c.get("course_name") or ""))
    except Exception:
        pass
    rows = []
    done = {}

    def one(item):
        cid, name = item
        if cid in done:
            return
        done[cid] = 1
        s2, acts = 0, []
        try:
            s2, t2, _ = ykt("GET", "/v2/api/web/logs/learn/" + cid +
                            "?actype=-1&page=0&offset=4&sort=-1", timeout=12, acct=acct)
            acts = ((json.loads(t2) or {}).get("data") or {}).get("activities") or []
        except Exception:
            acts = []
        for a in acts[:2]:
            rows.append({"cid": cid, "name": name, "title": a.get("title"),
                         "lessonId": a.get("courseware_id"), "createTime": a.get("create_time"),
                         "isFinished": a.get("is_finished"), "live": a.get("is_finished") is False,
                         "status": s2})

    for i in range(0, len(cids), 10):
        batch = [threading.Thread(target=one, args=(it,), daemon=True) for it in cids[i:i + 10]]
        for t in batch:
            t.start()
        for t in batch:
            t.join(timeout=18)
    data = {"courses": len(cids), "activities": rows,
            "live": [r for r in rows if r.get("live")], "at": int(now), "acct": acct}
    SCAN_CACHE[acct] = {"at": now, "data": data}
    return data


def live_now(acct="main"):
    """页面顶部"是否开课中"：主账号走两门课快查，其它账号复用全课扫描缓存。"""
    watch = CIDS_BY_ACCT.get(acct)
    if not watch:
        return [r for r in scan_all(False, acct)["activities"] if r.get("live")]
    out = []
    for cid in watch:
        st, text, _ = ykt("GET", "/v2/api/web/logs/learn/" + cid +
                          "?actype=-1&page=0&offset=6&sort=-1", timeout=12, acct=acct)
        try:
            acts = ((json.loads(text) or {}).get("data") or {}).get("activities") or []
        except Exception:
            acts = []
        for a in acts[:4]:
            out.append({"cid": cid, "title": a.get("title"), "lessonId": a.get("courseware_id"),
                        "createTime": a.get("create_time"), "isFinished": a.get("is_finished"),
                        "live": a.get("is_finished") is False, "status": st})
    return out


def live_all(acct="main"):
    """全局正在上课的课节（跨所有课堂）：服务端只下发 classroom_id，
    lessonId 还要按课堂再查一次 logs/learn。通配任务用它，避免漏课。"""
    st, text, _ = ykt("GET", "/api/v3/classroom/on-lesson-upcoming-exam", timeout=12, acct=acct)
    try:
        d = (json.loads(text) or {}).get("data") or {}
    except Exception:
        d = {}
    out = []
    for c in (d.get("onLessonClassrooms") or []):
        cid = str(c.get("classroomId") or c.get("classroom_id") or "")
        if not cid:
            continue
        lid = str(c.get("lessonId") or c.get("lesson_id") or "")
        if not lid:
            try:
                lid = live_lesson(acct, cid)
            except Exception:
                lid = ""
        if lid:
            out.append({"cid": cid, "lessonId": lid,
                        "title": c.get("courseName") or c.get("classroom_name") or "",
                        "cls": c.get("classroomName") or "", "live": True})
    return out


def _cookies(acct="main"):
    """凭据优先读 data/biz.db 的 ykt_accounts（cookie 字段仍是 secure 信封），
    文件只是历史兼容回落。"""
    try:
        import acct as A
        ck = A.cookie(acct)
        if ck and ck.get("sessionid"):
            return dict(ck)
    except Exception:
        pass
    name = "cookies.json" if acct in ("", "main", None) else os.path.join("accounts", str(acct) + ".json")
    d = secure.read_json(os.path.join(BASE, name), None)
    if d is None:
        raise FileNotFoundError(name)
    return d


ACCT_CACHE = {}


def acct_info(acct):
    hit = ACCT_CACHE.get(acct)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    st, text, _ = ykt("GET", "/api/v3/user/basic-info", timeout=10, acct=acct)
    info = {"status": st, "name": "", "school": "", "uid": ""}
    try:
        d = (json.loads(text) or {}).get("data") or {}
        info.update({"name": d.get("name") or "", "school": d.get("school") or "", "uid": d.get("id") or ""})
    except Exception:
        pass
    ACCT_CACHE[acct] = (time.time(), info)
    return info


def parse_cookie(s):
    """接受 {"sessionid":"..","sid":".."} 或 "sessionid=..; sid=.." 两种粘贴形式。"""
    s = (s or "").strip()
    if s.startswith("{"):
        d = json.loads(s)
    else:
        d = {}
        for part in s.replace(";", "\n").split("\n"):
            if "=" in part:
                k, v = part.split("=", 1)
                d[k.strip()] = v.strip()
    return {k: v for k, v in d.items() if k and v}


def accounts():
    items = [("main", "主账号")]
    try:
        for fn in sorted(os.listdir(os.path.join(BASE, "accounts"))):
            if fn.endswith(".json"):
                items.append((fn[:-5], fn[:-5]))
    except OSError:
        pass
    out = []
    for aid, label in items:
        info = acct_info(aid)
        out.append({"id": aid, "label": info.get("name") or label,
                    "name": info.get("name") or "", "status": info.get("status"),
                    "ok": info.get("status") == 200})
    return out


# ---- HTTP 连接复用（keep-alive）----
# 原来每次 urllib.urlopen 都新建 TCP + 完整 TLS 握手：一轮调度几十个请求就几十次握手，
# 既慢（每请求 +50~150ms）又在服务器上留一堆 TIME_WAIT。这里按「线程 + host」缓存一条
# 连接复用；服务端关掉连接（will_close / 异常）时自动重连一次，3xx 交回 urllib 保持原重定向语义。
_KA_ON = os.environ.get("YKT_KEEPALIVE", "1") == "1"
_KA = threading.local()
_TLS_CTX = [None]


def _tls_ctx():
    if _TLS_CTX[0] is None:
        import ssl
        _TLS_CTX[0] = ssl.create_default_context()
    return _TLS_CTX[0]


def _ka_drop():
    c = getattr(_KA, "c", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _KA.c = None
    _KA.k = None


def _http_req(method, url, data, headers, timeout):
    """返回 (status, text, headers_lower)。status=0 表示网络层失败，text 是错误串。"""
    import http.client
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", host)
    err = ""
    for attempt in (0, 1):
        c = None
        if _KA_ON:
            c = getattr(_KA, "c", None)
            if c is not None and getattr(_KA, "k", None) != (host, port):
                _ka_drop()
                c = None
        if c is None:
            try:
                if u.scheme == "https":
                    c = http.client.HTTPSConnection(host, port, timeout=timeout, context=_tls_ctx())
                else:
                    c = http.client.HTTPConnection(host, port, timeout=timeout)
            except Exception as exc:
                return 0, "transport error: " + str(exc), {}
        try:
            c.request(method, path, body=data, headers=hdrs)
            r = c.getresponse()
            raw = r.read()
            hl = {}
            try:
                hl = {k.lower(): v for k, v in r.getheaders()}
            except Exception:
                pass
            if 300 <= r.status < 400:
                if _KA_ON and not getattr(r, "will_close", True):
                    _KA.c, _KA.k = c, (host, port)
                else:
                    try:
                        c.close()
                    except Exception:
                        pass
                    _KA.c, _KA.k = None, None
                try:
                    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
                    resp = urllib.request.urlopen(req, timeout=timeout)
                    return resp.status, resp.read().decode("utf-8", "replace"), {k.lower(): v for k, v in resp.headers.items()}
                except urllib.error.HTTPError as e2:
                    return e2.code, e2.read().decode("utf-8", "replace"), ({k.lower(): v for k, v in e2.headers.items()} if e2.headers else {})
            if _KA_ON and not getattr(r, "will_close", True):
                _KA.c, _KA.k = c, (host, port)
            else:
                try:
                    c.close()
                except Exception:
                    pass
                _KA.c, _KA.k = None, None
            return r.status, raw.decode("utf-8", "replace"), hl
        except Exception as exc:
            err = str(exc)
            try:
                c.close()
            except Exception:
                pass
            _KA.c = None
            _KA.k = None
            if attempt == 0:
                continue
    return 0, "transport error: " + err, {}


def ykt(method, path, body=None, timeout=20, acct="main", auth="", app=False, headers_extra=None):
    """带会话打一次雨课堂接口，返回 (status, text, auth)。

    app=True 切换成移动端头（x-client: app + Android UA）——/api/v3/app/scan
    等 app 专属端点只认这套头，桌面头会被 400 打回。
    """
    ck = _cookies(acct)
    domain = safe_domain(ck.pop("__domain", "") or DOMAIN)
    url = "https://" + domain + path
    data = None
    if app:
        headers = {
            "User-Agent": APP_UA,
            "xtbz": "ykt",
            "x-client": "app",
            "deviceCode": DEVICE_CODE,
            "device-code": DEVICE_CODE,
            "Accept": "application/json, text/plain, */*",
        }
    else:
        headers = {
            "User-Agent": UA,
            "xtbz": "ykt",
            "Accept": "application/json, text/plain, */*",
        }
        if os.environ.get("YKT_DESKTOP", "0") == "1":
            # 老桌面头（desktop-v: v2 + X-Client: desktop + Origin: file://）。
            # 2026-11 之后新版 web 会话带这套头会让 /api/v3 直接 401「登录失效」，
            # 只带 xtbz + Referer 才是当前可用的组合。
            headers.update({"desktop-v": "v2", "X-Client": "desktop", "Origin": "file://"})
        else:
            headers["Referer"] = "https://" + domain + "/v2/web/index"
    if headers_extra:
        headers.update({k: v for k, v in headers_extra.items() if v})
    headers["Cookie"] = "; ".join(k + "=" + v for k, v in ck.items() if not k.startswith("__"))
    if auth:
        headers["Authorization"] = "Bearer " + auth
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    st, text, hl = _http_req(method, url, data, headers, timeout)
    if st == 0:
        return 0, text, ""
    return st, text, (hl.get("set-auth") or hl.get("Set-Auth") or "")


def mask(tok):
    return (tok[:8] + "..." + tok[-4:]) if tok and len(tok) > 14 else ("set" if tok else "")


def app_scan(raw, acct="main"):
    """把二维码整条 URL 交给 app 端 /api/v3/app/scan。

    桌面 cookie + app 头（x-client: app + Android UA）就能打通，无需 App 会话。
    返回 {ok, code, type, value, flow, status, resp}：
      type=checkin/lesson 时 value 可能是 lessonId；type=login 时 value 是登录会话 id。
    """
    out = {"ok": False, "code": None, "type": "", "value": "", "flow": "",
           "status": 0, "resp": "", "at": int(time.time())}
    raw = str(raw or "").strip()
    if not raw:
        out["resp"] = "empty"
        return out
    st, text, auth = ykt("POST", "/api/v3/app/scan", {"url": raw}, 15, acct, app=True)
    out["status"] = st
    out["resp"] = (text or "")[:400]
    out["auth"] = bool(auth)
    try:
        d = json.loads(text or "{}") or {}
    except Exception:
        return out
    out["code"] = d.get("code")
    data = d.get("data") or {}
    if isinstance(data, dict):
        out["type"] = str(data.get("type") or "")
        out["value"] = str(data.get("value") or "")
        out["flow"] = str(data.get("flow") or "")
    out["ok"] = out.get("code") == 0
    return out


def _replay_inner(raw, acct="main"):
    """把二维码内容变成一次真实签到尝试；任一形态拿到 lessonToken 即返回。"""
    out = {"raw": raw[:300], "steps": [], "ok": False, "token": "", "lessonId": "", "at": int(time.time())}
    u = urllib.parse.urlparse(raw)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    lid = q.get("lessonId") or q.get("lesson_id") or q.get("id") or ""
    out["lessonId"] = lid

    def try_step(tag, fn):
        try:
            status, text, auth = fn()
        except Exception as e:
            out["steps"].append({"tag": tag, "status": 0, "body": str(e)[:160], "auth": False})
            return False
        try:
            _code = json.loads(text or "{}").get("code")
        except Exception:
            _code = None
        ok = bool(auth) or _code == 0
        out["steps"].append({"tag": tag, "status": status, "body": (text or "")[:400], "auth": ok})
        if ok:
            out["ok"] = True
            out["token"] = auth
            with open(os.path.join(BASE, "lesson_auth.json"), "w", encoding="utf-8") as f:
                json.dump({"lessonId": lid, "auth": auth, "at": out["at"]}, f)
            return True
        return False

    if u.scheme in ("http", "https") and "yuketang" in (u.hostname or ""):
        if try_step("GET-url", lambda: ykt("GET", u.path + ("?" + u.query if u.query else ""), acct=acct)):
            return out

    if not lid:
        out["error"] = "二维码里没有 lessonId，请手动补 lessonId 或先在网页里选课程"
        return out

    if try_step("POST-enter", lambda: ykt("POST", "/api/v3/lesson/checkin",
                                         {"source": 5, "lessonId": str(lid)}, acct=acct)):
        return out
    return out


def replay(raw, acct="main"):
    """把二维码内容解析成一次签到请求；任一形态拿到 lessonToken 即视为成功。"""
    return _replay_inner(raw, acct)


def lesson_token(acct="main"):
    names = []
    if acct and acct != "main":
        names.append(os.path.join(BASE, "lesson_auth_" + str(acct) + ".json"))
    names.append(os.path.join(BASE, "lesson_auth.json"))
    for p in names:
        try:
            d = secure.read_json(p, None)
            if not d:
                continue
            tok = d.get("auth") or d.get("token") or ""
            if tok:
                return tok, str(d.get("lessonId") or "")
        except OSError:
            continue
    return "", ""



def add_account(label, cookie_text, domain=""):
    """写入一个账号并校验会话，返回 {ok,id,name,school,status}。"""
    label = re.sub(r"[^A-Za-z0-9_-]", "", str(label or ""))[:24] or ("acct" + str(int(time.time())))
    try:
        ck = parse_cookie(cookie_text or "")
    except Exception:
        ck = {}
    if not ck.get("sessionid"):
        return {"ok": False, "id": label, "status": 0, "msg": "缺少 sessionid"}
    dm = str(domain or "").strip()
    if dm:
        if not re.match(r"^[a-z0-9.-]+$", dm):
            return {"ok": False, "id": label, "status": 0, "msg": "domain 不合法"}
        ck["__domain"] = dm
    d = os.path.join(BASE, "accounts")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, label + ".json")
    bak = p + ".bak"
    old_bytes = None
    try:
        if os.path.exists(p):
            with open(p, "rb") as f:
                old_bytes = f.read()
            with open(bak, "wb") as f:
                f.write(old_bytes)
    except OSError:
        old_bytes = None
    secure.write_json(p, ck)
    ACCT_CACHE.pop(label, None)
    st, text, _ = ykt("GET", "/api/v3/user/basic-info", timeout=12, acct=label)
    info = acct_info(label)
    if not (st == 200 and (info.get("uid") or info.get("name"))):
        if old_bytes is not None:
            try:
                with open(p, "wb") as f:
                    f.write(old_bytes)
                ACCT_CACHE.pop(label, None)
            except OSError:
                pass
            return {"ok": False, "id": label, "status": st, "body": (text or "")[:200],
                    "restored": True,
                    "msg": "新 Cookie 没通过校验，已还原原来能用的绑定（别再贴这个了）"}
        try:
            os.remove(p)
        except OSError:
            pass
        return {"ok": False, "id": label, "status": st, "body": (text or "")[:200]}
    try:
        import acct as A
        A.put(label, ck, None, info.get("name") or "", info.get("school") or "",
              str(info.get("uid") or ""), dm or DOMAIN, int(st))
    except Exception:
        pass
    return {"ok": True, "id": label, "name": info.get("name") or "",
            "school": info.get("school") or "", "status": st}


def refresh_info(acct):
    """回读雨课堂真实姓名/学校并写库（绑定后 cookies 生效时补名用）。"""
    info = acct_info(acct)
    name = str(info.get("name") or "").strip()
    school = str(info.get("school") or "").strip()
    if not (name or school):
        return {"ok": False, "name": "", "school": "", "msg": "雨课堂没返回姓名，可能会话已过期" }
    try:
        import acct as A
        row = A.get(acct) or {}
        ck = A.cookie(acct) or {}
        A.put(acct, ck, row.get("owner_id"), name, school, str(info.get("uid") or ""),
              row.get("domain") or DOMAIN, int(info.get("status") or 200))
    except Exception as exc:
        return {"ok": False, "name": name, "school": school, "msg": str(exc)[:90]}
    return {"ok": True, "name": name, "school": school, "uid": str(info.get("uid") or "")}


def live_lesson(acct, cid):
    """该课堂是否正在上课，是则返回 lessonId（courseware_id）。"""
    st, text, _ = ykt("GET", "/v2/api/web/logs/learn/" + str(cid) +
                      "?actype=-1&page=0&offset=4&sort=-1", timeout=12, acct=acct)
    try:
        acts = ((json.loads(text) or {}).get("data") or {}).get("activities") or []
    except Exception:
        acts = []
    now_ms = int(time.time() * 1000)
    for a in acts[:3]:
        if a.get("is_finished") is not False or not a.get("courseware_id"):
            continue
        ct = int(a.get("create_time") or 0)
        if ct and now_ms - ct > 6 * 3600 * 1000:
            continue
        return str(a.get("courseware_id"))
    return ""


def list_accounts():
    """服务器上已登录的雨课堂账号（以 DB 为准）：acct / 昵称 / 归属 / 在线。"""
    out = []
    try:
        import acct as A
        rows = A.list_all()
    except Exception:
        rows = []
    for r in rows:
        info = {"name": r.get("name") or "", "school": r.get("school") or "",
                "status": int(r.get("status") or 0)}
        if not info["name"]:
            try:
                info = acct_info(r["acct"])
            except Exception:
                pass
        out.append({"acct": r["acct"], "label": r["acct"],
                    "name": str(info.get("name") or ""),
                    "school": str(info.get("school") or ""),
                    "status": int(info.get("status") or 0),
                    "online": bool(int(info.get("status") or 0) == 200 and info.get("name")),
                    "owner_id": r.get("owner_id")})
    return out


def term_label(term):
    """学期号转人话：202601 -> 2026 秋，202502 -> 2026 春（秋=秋季学期，春=次年春季）。"""
    try:
        t = int(term or 0)
    except Exception:
        t = 0
    if t <= 0:
        return "未标注"
    y, s = t // 100, t % 100
    if s == 1:
        return "%d 秋" % y
    if s == 2:
        return "%d 春" % (y + 1)
    return str(t)


# 学科全名 -> 课堂页显示名（用户指定口径，2026-11-16）
SUBJECT_ALIAS = {
    "C#面向对象程序设计": "C#面向对象",
    "EDA技术及应用": "EDA",
    "概率论与数理统计B": "概率论",
    "概率论与数理统计A": "概率论",
    "大学生劳动教育Ⅰ": "劳动教育Ⅰ",
    "大学生劳动教育Ⅱ": "劳动教育Ⅱ",
    "大学生劳动教育Ⅲ": "劳动教育Ⅲ",
    "大学生劳动教育Ⅳ": "劳动教育Ⅳ",
    "生成式人工智能原理及其影响和应用": "生成式人工智能",
    "现场生命急救知识与技能": "现场生命急救",
    "中国近现代史纲要": "中国近现代史纲要",
}

# 超星（学习通）课程的教师字段会写成“上课网站 http://xxx.chaoxing.com/”
CX_MARK = "chaoxing.com"

# 课堂页置顶顺序（用户 2026-11-16 指定，未列出的课程排在后面）
PIN_ORDER = ["C#面向对象", "EDA", "计算机组成原理", "数据结构与算法",
             "生成式人工智能（超星）", "现场生命急救（超星）", "劳动教育Ⅲ",
             "大学物理C", "大学物理实验B", "概率论", "大学英语BⅢ"]


def pin_rank(label):
    try:
        return PIN_ORDER.index(str(label or ""))
    except ValueError:
        return -1


def subject_label(subject, teacher=""):
    """学科全名 + 教师 -> 课堂页显示名（简写 + 超星标注）。"""
    s = str(subject or "").strip()
    s = SUBJECT_ALIAS.get(s, s)
    if s and CX_MARK in str(teacher or "") and "（超星）" not in s:
        s = s + "（超星）"
    return s


def acct_courses(acct):
    """列出该账号可见课堂（雨课堂按班级拆行，同一学科会是多行）。

    返回 cid / 学科 subject / 显示名 label / 班级名 name / 学期 / 教师 / 人数 / 角色 / 系列号。
    """
    st, text, _ = ykt("GET", "/v2/api/web/courses/list?identity=2", timeout=15, acct=acct)
    out = []
    try:
        data = (json.loads(text) or {}).get("data") or {}
        for c in (data.get("list") or []):
            cid = str(c.get("classroom_id") or c.get("id") or "")
            if not cid:
                continue
            t = c.get("teacher") or {}
            term = c.get("term") or 0
            cr = c.get("course") or {}
            subject = str(cr.get("name") or c.get("course_name") or "").strip()
            teacher_raw = t.get("name") or ""
            teacher = "超星" if CX_MARK in teacher_raw else teacher_raw
            out.append({"cid": cid,
                        "subject": subject,
                        "label": subject_label(subject, teacher_raw),
                        "name": c.get("name") or "",
                        "short": c.get("short_name") or "",
                        "term": term,
                        "term_label": term_label(term),
                        "teacher": teacher,
                        "students": int(c.get("students_count") or 0),
                        "role": c.get("role") or 0,
                        "series": c.get("university_course_series_id") or "",
                        "uni": int(cr.get("university_id") or 0),
                        "ctype": int(cr.get("type") or 0)})
    except Exception:
        pass
    out.sort(key=lambda x: (-int(x.get("term") or 0),
                            str(x.get("label") or ""),
                            str(x.get("teacher") or "")))
    return out


def group_courses(rows):
    """按「学科 + 学期」把多个教学班合并成一张卡片，供前端折叠展示。"""
    order, idx = [], {}
    for r in rows or []:
        try:
            term = int(r.get("term") or 0)
        except Exception:
            term = 0
        subject = str(r.get("subject") or "").strip() or str(r.get("name") or "")
        key = (subject, term)
        g = idx.get(key)
        if g is None:
            g = {"subject": subject,
                 "label": r.get("label") or subject_label(subject, r.get("teacher") or ""),
                 "name": r.get("name") or "",
                 "names": [],
                 "term": term,
                 "term_label": r.get("term_label") or term_label(term),
                 "classes": [], "teachers": [], "students": 0}
            idx[key] = g
            order.append(g)
        nm = str(r.get("name") or "")
        if nm and nm not in g["names"]:
            g["names"].append(nm)
        g["classes"].append({"cid": r.get("cid") or "",
                             "teacher": r.get("teacher") or "",
                             "cls": nm,
                             "students": int(r.get("students") or 0),
                             "series": r.get("series") or ""})
        t = r.get("teacher") or ""
        if t and t not in g["teachers"]:
            g["teachers"].append(t)
        g["students"] += int(r.get("students") or 0)
    for g in order:
        g["n"] = len(g["classes"])
        g["pin"] = pin_rank(g.get("label"))

    def _key(g):
        p = g.get("pin", -1)
        return (0, p, "") if p >= 0 else (1, -int(g.get("term") or 0), str(g.get("label") or ""))

    order.sort(key=_key)
    return order


# 签到入口：46 通用 / 5 普通 / 21 扫码 / 42 App 扫码 / 14 暗号
# 实测 2026-11-16 19:26（中国近现代史纲要 lessonId 1821000000000000002）：
#   source=5  → {"code":50070,"msg":"DYNAMIC_QR_CHECK_IN_REFUSED"}
#   source=46 → {"code":0,"msg":"OK"} + Set-Auth
# 签到入口 source 取值：46 通用 / 5 普通 / 21 扫码跳转 / 42 App 扫码
SRC_CHAIN = (46, 5, 21, 42)


def _tprobe(msg):
    try:
        with open("/opt/yktsign/data/teacher_probe.log", "a", encoding="utf-8") as f:
            f.write("%d %s\n" % (int(time.time()), msg))
    except Exception:
        pass


def do_checkin(acct, lid, source=None, only=False):
    """对指定课节打一次签到，返回 (ok, detail)。

    显式 source 优先，失败后按 SRC_CHAIN 依次回退；首个 code 0（或拿到 Set-Auth）即成功。
    only=True 时只打显式 source 这一个入口（快探用：单发约 0.33s，省掉三次往返）。
    """
    seq = []
    try:
        if source not in (None, "", 0, "0"):
            seq.append(int(source))
    except Exception:
        pass
    if not only:
        for s in SRC_CHAIN:
            if s not in seq:
                seq.append(s)
    if not seq:
        seq = [46]
    notes = []
    for s in seq:
        body = {"source": s, "lessonId": str(lid)}
        # 2026-11-18 移除两个负资产：

        #     留着只是一份「意图跳过校验」的书面证据。
        st, text, auth = ykt("POST", "/api/v3/lesson/checkin", body, acct=acct)
        try:
            code = json.loads(text or "{}").get("code")
        except Exception:
            code = None
        if auth or code == 0:
            if auth:
                name = ("lesson_auth.json" if acct in ("", "main", None)
                        else ("lesson_auth_" + str(acct) + ".json"))
                with lock:
                    secure.write_json(os.path.join(BASE, name),
                                      {"lessonId": str(lid), "auth": auth, "at": int(time.time())})
            return True, ("source=" + str(s) + " status=" + str(st) + " " + (text or "")[:150])
        notes.append("s" + str(s) + ":" + str(code if code is not None else st) + ("+A" if auth else ""))
        try:
            with open("/opt/yktsign/data/checkin_diag.jsonl", "a", encoding="utf-8") as _df:
                _df.write(json.dumps({"at": int(time.time()), "acct": acct, "lid": str(lid),
                                      "s": s, "st": st, "code": code, "auth": bool(auth),
                                      "body": (text or "")[:200]}, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return False, "all sources failed · " + " | ".join(notes)[:280]


TT_CACHE = {}
TT_LOCK = __import__("threading").Lock()


TT_ACTS = {}

# ── 课表磁盘缓存（2026-11-17 夜间优化）───────────────────────────────
# 冷启动要先拉 courses 再逐课堂拉 logs/learn，最慢实测 5.5s；进程一重启内存缓存全丢。
# 这里把算好的课表落盘，进程起来先读回，再在后台把缺的账号预热一遍。
TT_DISK = os.environ.get("YKT_TT_DISK") or "/opt/yktsign/data/tt_cache.json"
TT_TTL = 21600
TT_SVC_LOG = os.environ.get("YKT_SVC_LOG") or "/opt/yktsign/svc.log"
TT_DIRTY = {"v": False}
TT_BUSY = {}


def _tt_busy(acct):
    """该账号是否已有线程在算课表（60s 内视为在算，防异常卡死）。"""
    with TT_LOCK:
        at = TT_BUSY.get(acct)
    return bool(at and time.time() - at < 60)


def _tt_mark(acct, on):
    with TT_LOCK:
        if on:
            TT_BUSY[acct] = time.time()
        else:
            TT_BUSY.pop(acct, None)


def _tt_log(msg):
    try:
        with open(TT_SVC_LOG, "a", encoding="utf-8") as f:
            f.write("%d [tt] %s\n" % (int(time.time()), msg))
    except Exception:
        pass


def tt_load():
    """把磁盘课表读回内存（只认 TTL 内的），返回读回条数。"""
    try:
        with open(TT_DISK, "r", encoding="utf-8") as f:
            js = json.load(f)
    except Exception:
        return 0
    now = time.time()
    n = 0
    with TT_LOCK:
        for acct, item in (js.get("accts") or {}).items():
            try:
                at = float(item[0])
                out = item[1]
            except Exception:
                continue
            if now - at < TT_TTL and isinstance(out, dict):
                TT_CACHE[acct] = (at, out)
                n += 1
    if n:
        _tt_log("tt_load " + str(n) + " accts from disk")
    return n


def tt_save():
    """原子落盘：写 .tmp 再 os.replace，避免留下半截 JSON。"""
    now = time.time()
    with TT_LOCK:
        payload = {"at": int(now), "ttl": TT_TTL,
                   "accts": {k: [v[0], v[1]] for k, v in TT_CACHE.items()
                             if now - v[0] < TT_TTL}}
    try:
        d = os.path.dirname(TT_DISK)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = TT_DISK + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, TT_DISK)
        TT_DIRTY["v"] = False
        return True
    except Exception as exc:
        _tt_log("tt_save failed " + repr(exc))
        return False


def tt_accts():
    """需要预热的账号：已绑定雨课堂账号的去重列表。"""
    out = []
    try:
        import acct as A
        for r in A.list_all():
            a = str(r.get("acct") or "")
            if a and a not in out:
                out.append(a)
    except Exception as exc:
        _tt_log("tt_accts failed " + repr(exc))
    return out


def warm_timetable(only_cold=True):
    """后台把账号课表算热；only_cold 时跳过缓存仍有效的账号。"""
    now = time.time()
    done = []
    for a in tt_accts():
        if only_cold:
            with TT_LOCK:
                hit = TT_CACHE.get(a)
            if hit and now - hit[0] < TT_TTL - 600:
                continue
        t0 = time.time()
        try:
            out = timetable(a)
            done.append((a, len(out.get("slots") or []), round(time.time() - t0, 2)))
        except Exception as exc:
            _tt_log("warm fail acct=" + str(a) + " " + repr(exc))
    if done:
        _tt_log("tt_warm " + repr(done))
        tt_save()          # 预热完立刻落盘：进程若在 120s 内再重启也不会丢
    return done


def tt_boot():
    """进程启动：读回磁盘缓存 → 起自动落盘线程 → 后台预热冷账号。"""
    tt_load()

    def autosave():
        while True:
            time.sleep(60)
            if not TT_DIRTY["v"]:
                continue
            try:
                tt_save()
            except Exception:
                pass

    threading.Thread(target=autosave, daemon=True).start()

    def warm():
        try:
            warm_timetable()
        except Exception as exc:
            _tt_log("warm thread failed " + repr(exc))

    threading.Thread(target=warm, daemon=True).start()
    return True


def learn_acts(acct, cid, offset=20, ttl=21600):
    """某课堂最近的学习记录（activities），带 create_time(ms)=开课时间。"""
    hit = TT_ACTS.get((acct, str(cid)))
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        st, txt, _ = ykt("GET", "/v2/api/web/logs/learn/" + str(cid) +
                         "?actype=-1&page=0&offset=" + str(int(offset)) + "&sort=-1", acct=acct)
        js = json.loads(txt or "{}")
        acts = (js.get("data") or {}).get("activities") or []
        if acts:
            TT_ACTS[(acct, str(cid))] = (time.time(), acts)
        return acts
    except Exception:
        return hit[1] if hit else []


def timetable(acct, days=42, ttl=TT_TTL):
    """按历史课节反推周课表：把每节课的开课时间归到 (周几, 时:分)。

    雨课堂没有"课表"接口，只能用 logs/learn 里每节的 create_time 统计规律；
    同一 (学科, 周几, HH:MM) 命中 ≥1 次即视为固定课，命中次数作为可信度。
    days 控制回看天数。返回 {slots:[...], courses:N, at:ts, days:N}
    """
    now = time.time()
    with TT_LOCK:
        hit = TT_CACHE.get(acct)
    if hit and now - hit[0] < ttl:
        return hit[1]
    # 同一账号已有线程在算：等它 20s，别把上游打两遍（冷启动 5s 的请求经常并发到达）
    if _tt_busy(acct):
        for _ in range(200):
            time.sleep(0.1)
            with TT_LOCK:
                hit = TT_CACHE.get(acct)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            if not _tt_busy(acct):
                break
    _tt_mark(acct, True)
    rows = acct_courses(acct) or []
    label_of = {}
    subject_of = {}
    for r in rows:
        cid = str(r.get("cid") or "")
        if not cid or cid in label_of:
            continue
        # role=6 是旁听/非本班：不进课表，否则「今日课程」会混进别人的课
        if str(r.get("role") or "") == "6":
            continue
        label_of[cid] = r.get("label") or r.get("subject") or r.get("name") or cid
        subject_of[cid] = r.get("subject") or label_of[cid]
    acts_of = {}
    lock2 = __import__("threading").Lock()

    def work(cid):
        a = learn_acts(acct, cid, 20)
        with lock2:
            acts_of[cid] = a

    thr = []
    cids = list(label_of.keys())
    for cid in cids:
        t = __import__("threading").Thread(target=work, args=(cid,), daemon=True)
        thr.append(t)
        t.start()
        while True:                       # 并发上限 8，控制对上游的请求压力
            alive = [x for x in thr if x.is_alive()]
            if len(alive) < 8:
                break
            time.sleep(0.05)
    for t in thr:
        t.join(20)
    cutoff = now - days * 86400
    raw = []
    slots = {}
    for cid, acts in acts_of.items():
        for a in acts:
            # 只认「上课」这一种活动：type 14 带 attend_status；
            # 作业发布(19)/通知(9)/试卷(5) 也有 create_time，会把它们当成一节课
            if ("attend_status" not in a) and int(a.get("type") or 0) != 14:
                continue
            try:
                ms = int(a.get("create_time") or 0)
            except Exception:
                continue
            if not ms or ms / 1000.0 < cutoff:
                continue
            dt = time.localtime(ms / 1000.0)
            wd = dt.tm_wday + 1
            tmin = dt.tm_hour * 60 + dt.tm_min
            raw.append({"label": label_of.get(cid, cid), "subject": subject_of.get(cid, cid),
                        "cid": cid, "wd": wd, "min": tmin, "ms": ms,
                        "lid": str(a.get("courseware_id") or ""),
                        "title": (a.get("title") or "")[:60],
                        "attend": bool(a.get("attend_status")),
                        "finished": bool(a.get("is_finished"))})
    # 同一 (学科, 周几) 内把间隔 <=60 分钟的课归为同一节（create_time 是课件发布时间，不是整点）
    byday = {}
    for r in raw:
        byday.setdefault((r["label"], r["wd"]), []).append(r)
    for (label, wd), items in byday.items():
        items.sort(key=lambda x: x["min"])
        cluster = [items[0]]
        groups = []
        for it in items[1:]:
            if it["min"] - cluster[-1]["min"] <= 60:
                cluster.append(it)
            else:
                groups.append(cluster)
                cluster = [it]
        groups.append(cluster)
        for g in groups:
            mins = sorted(x["min"] for x in g)
            med = mins[len(mins) // 2]
            med = int(round(med / 5.0) * 5)
            start = "%02d:%02d" % (med // 60, med % 60)
            lessons = sorted(g, key=lambda x: -x["ms"])[:4]
            slots[(label, wd, start)] = {
                "label": label, "subject": g[0]["subject"], "cid": g[0]["cid"],
                "wd": wd, "start": start, "n": len(g),
                "lessons": [{"lessonId": x["lid"], "title": x["title"], "at": x["ms"],
                             "attend": x["attend"], "finished": x["finished"]} for x in lessons]}
    out = {"slots": sorted(slots.values(), key=lambda x: (x["wd"], x["start"], x["label"])),
           "courses": len(label_of), "days": int(days), "at": int(now)}
    with TT_LOCK:
        TT_CACHE[acct] = (now, out)
    TT_DIRTY["v"] = True
    _tt_mark(acct, False)
    return out


def attend_state(acct, cid, lid):
    """查该课节的学习记录：{attend, finished, title}。查不到/解析失败返回 None 字段。

    重要：老师尚未发起签到时 checkin 也可能返回 code 0，但考勤不计入 —— 必须回读这条记录确认，
    否则调度器会把「已提交」当成「已签到」并冷却 900 秒，正好错过真正的签到窗口。
    """
    out = {"attend": None, "finished": None, "title": ""}
    if not cid or not lid:
        return out
    try:
        st, txt, _ = ykt("GET", "/v2/api/web/logs/learn/" + str(cid) +
                         "?actype=-1&page=0&offset=20&sort=-1", acct=acct)
        js = json.loads(txt or "{}")
        acts = (js.get("data") or {}).get("activities") or []
        for a in acts:
            if str(a.get("courseware_id")) == str(lid):
                out["attend"] = bool(a.get("attend_status"))
                out["finished"] = bool(a.get("is_finished")) if a.get("is_finished") is not None else None
                out["title"] = a.get("title") or ""
                break
    except Exception:
        pass
    return out


