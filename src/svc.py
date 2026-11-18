"""svc.py — 核心服务端（协议/账号/签到/跟课/位置），不服务任何页面。

监听 127.0.0.1:8090，仅内网可达；调用方必须带 X-Svc-Token（svc_token.txt）。
后台（web.py）只通过 HTTP 调这里，不直接接触雨课堂 cookie 与协议细节。
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import secure            # 落盘加密 + 完整性校验
secure.boot()
proto = __import__("server")   # 协议层
try:
    loc = __import__("loc")
except Exception:
    loc = __import__("loc")
try:
    codepool = __import__("codepool")     # 码池：一次取码 → 全部账号
except Exception:
    codepool = None
secure.migrate_all()

TOKEN_FILE = os.path.join(BASE, "svc_token.txt")
EV = os.environ.get("YKT_EVENTS") or "/opt/yktsign/events"
WATCH = {}
WLOCK = threading.Lock()
WATCH_MAX_PER_ACCT = 3      # 单账号最多同时跟几节课
WATCH_MAX_TOTAL = 8         # 全站最多同时跟几节课（每课一个 python 进程）
LOGF = os.path.join(BASE, "svc.log")


def log(msg):
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write("%d %s\n" % (int(time.time()), msg))
    except OSError:
        pass


def token():
    try:
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
    except OSError:
        t = ""
    if not t:
        t = secrets.token_hex(24)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
    return t


TOKEN = token()


def ev_lines(lid, n=60):
    lid = re.sub(r"[^0-9]", "", str(lid or ""))     # 课节号只可能是数字：净化后拼接，防 ../ 穿越
    if not lid:
        return []
    p = os.path.join(EV, lid + ".jsonl")
    try:
        rows = [x for x in open(p, encoding="utf-8").readlines()[-n:] if x.strip()]
    except OSError:
        return []
    out = []
    for x in rows:
        try:
            out.append(json.loads(x))
        except Exception:
            pass
    return out


def _key(acct, lid):
    return str(acct) + ":" + str(lid)


def watch_start(acct, lid, secs=3600):
    k = _key(acct, lid)
    try:
        secs = max(60, min(int(secs or 3600), 14400))
    except Exception:
        secs = 3600
    with WLOCK:
        cur = WATCH.get(k)
        if cur and cur["proc"].poll() is None:
            return {"ok": True, "running": True, "pid": cur["proc"].pid, "reuse": True}
        for _k in [x for x, v in WATCH.items() if v["proc"].poll() is not None]:
            WATCH.pop(_k, None)
        alive = len(WATCH)
        mine = len([1 for x in WATCH if x.split(":")[0] == str(acct)])
        if alive >= WATCH_MAX_TOTAL:
            return {"ok": False, "msg": "服务器同时在跟的课太多了（%d 节上限），稍后再试" % WATCH_MAX_TOTAL}
        if mine >= WATCH_MAX_PER_ACCT:
            return {"ok": False, "msg": "这个账号同时最多跟 %d 节课" % WATCH_MAX_PER_ACCT}
        os.makedirs(EV, exist_ok=True)
        _outp = os.path.join(EV, "watch_" + k.replace(":", "_") + ".out")
        try:
            if os.path.getsize(_outp) > 2 * 1024 * 1024:   # 长子进程会把 .out 喂到几 GB，超 2MB 换新
                os.replace(_outp, _outp + ".1")
        except OSError:
            pass
        out = open(_outp, "ab")
        _wp = os.path.join(BASE, "lesson_watch.pyc")
        if not os.path.exists(_wp):
            _wp = os.path.join(BASE, "lesson_watch.py")
        proc = subprocess.Popen([sys.executable, _wp,
                                 str(acct), str(lid), str(int(secs))],
                                stdout=out, stderr=subprocess.STDOUT, cwd=BASE)
        try:
            out.close()      # 子进程已继承 fd，父进程不关就会每跟一节课漏一个句柄
        except OSError:
            pass
        WATCH[k] = {"proc": proc, "at": int(time.time()), "log": _outp}
    log("watch start " + k + " pid=" + str(proc.pid))
    return {"ok": True, "running": True, "pid": proc.pid}


def watch_stop(acct, lid):
    k = _key(acct, lid)
    with WLOCK:
        cur = WATCH.get(k)
        if not cur:
            return {"ok": True, "running": False}
        try:
            cur["proc"].terminate()
        except Exception:
            pass
        WATCH.pop(k, None)
    log("watch stop " + k)
    return {"ok": True, "running": False}


def watch_status(acct, lid):
    k = _key(acct, lid)
    cur = WATCH.get(k)
    running = bool(cur and cur["proc"].poll() is None)
    frames = ev_lines(lid, 400)
    return {"ok": True, "running": running, "pid": (cur["proc"].pid if running else None),
            "startedAt": (cur["at"] if cur else None), "frames": len(frames),
            "lastOps": [str(f.get("op") or f.get("raw", ""))[:40] for f in frames[-8:]
                        if isinstance(f, dict)]}


def _accounts():
    return proto.list_accounts() or []


HANDLERS = {}


def h(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


@h("/health")
def _f(v, d=0.0):
    """入参一律可能是字符串/None/乱码：转不动就落默认值，别把 500 抛给调用方。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def _i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(d)


def _health(b):
    return {"ok": True, "pid": os.getpid(), "domain": proto.DOMAIN, "accounts": len(_accounts())}


RAW_OK = ("/api/v3/", "/api/v2/", "/v2/api/", "/api/web/", "/api/v1/")


@h("/raw")
def _raw(b):
    p = b.get("path") or ""
    if not any(p.startswith(x) for x in RAW_OK) or ".." in p or "\n" in p:
        return {"ok": False, "error": "path not allowed", "status": 403, "text": "", "auth": ""}
    st, text, auth = proto.ykt(b.get("method") or "GET", b.get("path") or "",
                               b.get("body"), _f(b.get("timeout") or 20, 20.0),
                               b.get("acct") or "main", b.get("auth") or "",
                               app=bool(b.get("app")))
    return {"ok": True, "status": st, "text": text, "auth": auth}


@h("/accounts/list")
def _acc_list(b):
    return {"ok": True, "accounts": _accounts()}


@h("/accounts/add")
def _acc_add(b):
    return proto.add_account(b.get("label") or "", b.get("cookie") or "", b.get("domain") or "")


@h("/courses")
def _courses(b):
    rows = proto.acct_courses(b.get("acct") or "main")
    try:
        groups = proto.group_courses(rows)
    except Exception as exc:
        log("group_courses failed " + repr(exc))
        groups = []
    return {"ok": True, "courses": rows, "groups": groups}


@h("/attend")
def _attend(b):
    try:
        r = proto.attend_state(b.get("acct") or "main", str(b.get("cid") or ""), str(b.get("lid") or ""))
        return {"ok": True, **(r or {})}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)[:160], "attend": None}


@h("/timetable")
def _timetable(b):
    acct = b.get("acct") or "main"
    try:
        return {"ok": True, "tt": proto.timetable(acct, _i(b.get("days") or 42, 42))}
    except Exception as exc:
        log("timetable failed " + repr(exc))
        return {"ok": False, "msg": str(exc)[:160], "tt": {"slots": [], "courses": 0}}


@h("/live")
def _live(b):
    return {"ok": True, "live": proto.live_now(b.get("acct") or "main")}


@h("/live-all")
def _live_all(b):
    try:
        return {"ok": True, "live": proto.live_all(b.get("acct") or "main")}
    except Exception as e:
        log("live_all err " + repr(e))
        return {"ok": False, "live": []}


@h("/live-lesson")
def _live_lesson(b):
    return {"ok": True, "lessonId": proto.live_lesson(b.get("acct") or "main", b.get("cid") or "")}


@h("/scan")
def _scan(b):
    return proto.scan_all(bool(b.get("force")), b.get("acct") or "main")


@h("/checkin")
def _checkin(b):
    ok, detail = proto.do_checkin(b.get("acct") or "main", str(b.get("lid") or ""),
                                  _i(b.get("source") or 5, 5))
    return {"ok": ok, "detail": detail}


@h("/replay")
def _replay(b):
    return proto.replay(b.get("raw") or "", b.get("acct") or "main")


@h("/scan-exchange")
def _scan_exchange(b):
    raw = b.get("raw") or ""
    accts = b.get("accts") or None
    if isinstance(accts, str):
        accts = [accts]
    res = loc.scan_and_checkin(raw, accts, _i(b.get("source") or 21, 21))
    rows = []
    for a, v in (res.get("per_acct") or {}).items():
        rows.append({"acct": a, "ok": bool(v.get("ok")), "lessonId": res.get("lessonId"),
                     "code": v.get("code"), "hint": (res.get("scan") or {}).get("hint", ""),
                     "detail": v.get("hint") or ""})
    return {"ok": bool(res.get("lessonId")), "via": "svc-scan",
            "lessonId": res.get("lessonId"), "results": rows}


@h("/pool/add")
def _pool_add(b):
    if codepool is None:
        return {"ok": False, "msg": "codepool missing"}
    return codepool.add(b.get("raw") or "", b.get("by") or b.get("acct") or "")


@h("/pool/status")
def _pool_status(b):
    if codepool is None:
        return {"ok": False, "pool": []}
    return codepool.status()


@h("/pool/try")
def _pool_try(b):
    if codepool is None:
        return {"ok": False, "tried": [], "left": 0}
    accts = b.get("accts") or None
    if isinstance(accts, str):
        accts = [accts]
    return codepool.try_all(_i(b.get("source") or 21, 21), accts, bool(b.get("force")))


@h("/pool/drop")
def _pool_drop(b):
    if codepool is None:
        return {"ok": False}
    return codepool.drop(bool(b.get("all")), b.get("raw") or "")


@h("/geo/eligibility")
def _geo_elig(b):
    return loc.loc_eligibility(b.get("acct") or "main", str(b.get("lid") or ""), b.get("auth") or "")


@h("/geo/preview")
def _geo_prev(b):
    return loc.loc_preview(b.get("acct") or "main", str(b.get("lid") or ""),
                           b.get("lat"), b.get("lng"), b.get("auth") or "")


@h("/geo/submit")
def _geo_sub(b):
    return loc.loc_submit(b.get("acct") or "main", str(b.get("lid") or ""), b.get("lat"), b.get("lng"),
                          b.get("address") or "", b.get("auth") or "", _i(b.get("source") or 54, 54),
                          bool(b.get("gcj", True)))


@h("/geo/auto")
def _geo_auto(b):
    """位置签到全自动：按 eligibility 的中心点+半径自己算坐标，不需要人给经纬度。"""
    return loc.geo_auto(b.get("acct") or "main", str(b.get("lid") or ""),
                        b.get("auth") or "", b.get("address") or "",
                        _f(b.get("frac") or 0.45, 0.45))


@h("/geo/flow")
def _geo_flow(b):
    acct = b.get("acct") or "main"
    lid = str(b.get("lid") or "")
    lat, lng = b.get("lat"), b.get("lng")
    auth = loc._auth_for(acct, lid, b.get("auth") or "")
    out = {"acct": acct, "lid": lid, "ok": False, "authed": bool(auth), "gcj02": bool(b.get("gcj", True))}
    try:
        el = loc.loc_eligibility(acct, lid, auth)
        out["eligibility"] = el
        out["needLocationSignIn"] = (el.get("cfg") or {}).get("needLocationSignIn")
    except Exception as exc:
        out["eligibility"] = {"ok": False, "resp": repr(exc)}
    try:
        glat, glng = loc.wgs2gcj(lat, lng) if b.get("gcj", True) else (float(lat), float(lng))
        out["coords"] = {"input": [float(lat), float(lng)], "used": [glat, glng]}
        out["preview"] = loc.loc_preview(acct, lid, glat, glng, auth)
    except Exception as exc:
        out["preview"] = {"ok": False, "resp": repr(exc)}
    try:
        sb = loc.loc_submit(acct, lid, lat, lng, b.get("address") or "", auth, 54, bool(b.get("gcj", True)))
        out["submit"] = sb
        out["ok"] = bool(sb.get("ok"))
        out["code"] = sb.get("code")
    except Exception as exc:
        out["submit"] = {"ok": False, "resp": repr(exc)}
    return out


@h("/watch/start")
def _w_start(b):
    return watch_start(b.get("acct") or "main", str(b.get("lid") or ""), int(b.get("secs") or 3600))


@h("/watch/stop")
def _w_stop(b):
    return watch_stop(b.get("acct") or "main", str(b.get("lid") or ""))


@h("/watch/status")
def _w_status(b):
    return watch_status(b.get("acct") or "main", str(b.get("lid") or ""))


class H(BaseHTTPRequestHandler):
    server_version = "ykt-svc/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/health", "/svc/health"):
            return self._send(200, _health({}))
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/svc/"):
            p = p[4:]
        if self.headers.get("X-Svc-Token", "") != TOKEN:
            return self._send(403, {"ok": False, "error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return self._send(400, {"ok": False, "error": "bad content-length"})
        if n < 0 or n > 1024 * 1024:
            return self._send(413, {"ok": False, "error": "body too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        fn = HANDLERS.get(p)
        if not fn:
            return self._send(404, {"ok": False, "error": "unknown endpoint " + p})
        try:
            return self._send(200, fn(body or {}))
        except Exception as exc:
            log("svc error " + p + " " + repr(exc))
            return self._send(500, {"ok": False, "error": repr(exc)})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), H)
    try:
        proto.tt_boot()      # 读回磁盘课表缓存 + 后台预热（2026-11-17 夜间优化）
    except Exception as exc:
        log("tt_boot failed " + repr(exc))
    print("ykt-svc listening on %s:%d domain=%s" % (host, port, proto.DOMAIN), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
