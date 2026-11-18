"""appsession.py — 复刻雨课堂 Android App 的鉴权信封，并探测端点可达性。

依据：对官方 App 抓包所得的字段级对齐结果
  · 响应拦截：Set-Cookie: csrftoken → 静态 0xd58 + 头 X-CSRFToken + saveData("token")
              Set-Cookie: sessionid → 静态 0xd5c + 头 sessionid + saveData("sessionid")
              响应头 Set-Auth → config.field_7b
  · 请求拦截：X-UID = userInfo.id；config.field_7b 非空 → Authorization: Bearer <field_7b>
  · Cookie 头 = "csrftoken=<0xd58>; sessionid=<0xd5c>; sid=<0xd60>; xtbbz=ykt"
  · 结论：App 的 Bearer **就是签到/课程响应下发的 Set-Auth**（JWT，含 l/c/u/i/p/r/exp），
    不存在独立的 App 登录 token。

用法：
  python3 -OO appsession.py info            # 取 App 侧用户信息（验证信封有效）
  python3 -OO appsession.py probe           # 端点可达性矩阵（学生会话）
  python3 -OO appsession.py call <PATH> [--bearer <tok>] [--uid N] [--sid S]
"""
import json
import sys
import urllib.error
import urllib.request

import server

APP_UA = server.APP_UA


def _ck(acct="main"):
    ck = dict(server._cookies(acct) or {})
    domain = str(ck.pop("__domain", "") or server.DOMAIN)
    return ck, domain


def cookie_of(acct="main", sid="", xtbbz=True):
    ck, _ = _ck(acct)
    parts = ["csrftoken=" + str(ck.get("csrftoken", "")), "sessionid=" + str(ck.get("sessionid", ""))]
    if sid:
        parts.append("sid=" + str(sid))
    if xtbbz:
        parts.append("xtbbz=ykt")
    return "; ".join(parts)


def call(acct, method, path, body=None, bearer="", uid=None, sid=None, xtbbz=True,
         extra=None, timeout=25):
    """完全照 App 的头组装；返回 (status, text, setauth)。"""
    ck, domain = _ck(acct)
    headers = {
        "User-Agent": APP_UA,
        "xtbz": "ykt",
        "x-client": "app",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie_of(acct, sid or "", xtbbz),
        "X-CSRFToken": str(ck.get("csrftoken", "")),
    }
    if uid:
        headers["X-UID"] = str(uid)
    if bearer:
        headers["Authorization"] = "Bearer " + str(bearer)
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("https://" + domain + path, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode("utf-8", "replace"), (resp.headers.get("Set-Auth") or "")
    except urllib.error.HTTPError as e:
        sa = e.headers.get("Set-Auth") if e.headers else ""
        return e.code, e.read().decode("utf-8", "replace"), (sa or "")
    except Exception as e:
        return 0, "transport error: " + str(e), ""


def _brief(text, n=170):
    t = (text or "").replace("\n", " ")
    return t[:n]


def info(acct="main"):
    out = {}
    for path in ("/api/v3/user/info", "/api/v3/user/user-info", "/api/v3/user/me", "/api/v3/user/profile"):
        st, text, sa = call(acct, "GET", path)
        out[path] = {"status": st, "body": _brief(text), "setauth": bool(sa)}
        try:
            d = json.loads(text or "{}")
            if d.get("code") == 0:
                out[path]["data"] = d.get("data")
                break
        except Exception:
            pass
    return out


def probe(acct="main", lid="", bearer=""):
    """学生会话下，App 信封对各类端点的可达性。"""
    rows = []
    paths = [
        ("普通签到详情(教师)", "/api/v3/lesson/checkin/detail?lessonId=" + lid),
        ("签到名单(教师)", "/api/v3/lesson/checkin-list?lessonId=" + lid),
        ("未签到名单(教师)", "/api/v3/lesson/uncheckin-list?lessonId=" + lid),
        ("位置签到规则", "/api/v3/lesson/location-checkin/eligibility?lessonId=" + lid),
        ("进行中课堂", "/api/v3/classroom/on-lesson-upcoming-exam"),
        ("课程列表", "/v2/api/web/courses/list?identity=2"),
    ]
    for tag, path in paths:
        for mode, kw in (("cookie", {}), ("cookie+X-UID", {}), ("cookie+UID+Bearer", {"bearer": bearer})):
            st, text, sa = call(acct, "GET", path, **kw)
            code = None
            try:
                code = json.loads(text or "{}").get("code")
            except Exception:
                pass
            rows.append({"endpoint": tag, "mode": mode, "status": st, "code": code,
                         "setauth": bool(sa), "body": _brief(text, 120)})
    return rows


def main():
    acct = "main"
    lid = ""
    args = [a for a in sys.argv[1:]]
    cmd = args[0] if args else "info"
    if "--acct" in args:
        acct = args[args.index("--acct") + 1]
    if "--lid" in args:
        lid = args[args.index("--lid") + 1]
    bearer = ""
    if "--bearer" in args:
        bearer = args[args.index("--bearer") + 1]
    if cmd == "info":
        print(json.dumps(info(acct), ensure_ascii=False, indent=1)[:2500])
    elif cmd == "probe":
        for r in probe(acct, lid, bearer):
            print("%-16s %-16s st=%s code=%s %s" % (r["endpoint"], r["mode"], r["status"], r["code"], r["body"]))
    elif cmd == "call":
        path = args[1]
        st, text, sa = call(acct, "GET", path, bearer=bearer)
        print("status", st, "setauth", bool(sa), "\n", text[:1500])
    else:
        print("usage: appsession.py info|probe|call <path> [--acct A] [--lid L] [--bearer T]")


if __name__ == "__main__":
    main()
