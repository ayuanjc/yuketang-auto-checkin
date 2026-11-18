"""svcclient.py — 后台调用核心服务端的瘦客户端（HTTP，token 鉴权）。"""
import http.client
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("YKT_SVC_URL") or "http://127.0.0.1:8090"


def _token():
    try:
        return open(os.path.join(BASE, "svc_token.txt"), encoding="utf-8").read().strip()
    except OSError:
        return ""


class SvcError(RuntimeError):
    pass


_KA = threading.local()


def call(ep, body=None, timeout=60):
    """调本地 svc（8090）。复用 keep-alive 连接：一轮调度上百次调用不再建/拆套接字，
    svc 侧也就不用为每个请求新建线程（ThreadingHTTPServer 的线程数直接跟着掉）。"""
    data = json.dumps(body or {}).encode()
    u = urllib.parse.urlparse(URL)
    host, port = u.hostname or "127.0.0.1", u.port or 80
    headers = {"Content-Type": "application/json", "X-Svc-Token": _token()}
    last = None
    for attempt in (0, 1):
        c = getattr(_KA, "c", None)
        if c is None:
            try:
                c = http.client.HTTPConnection(host, port, timeout=timeout)
            except Exception as exc:
                raise SvcError("svc unreachable: " + str(exc))
            _KA.c = c
        try:
            c.request("POST", ep, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
            if getattr(r, "will_close", False):
                try:
                    c.close()
                except Exception:
                    pass
                _KA.c = None
            return json.loads(raw.decode() or "{}")
        except Exception as exc:
            last = exc
            try:
                c.close()
            except Exception:
                pass
            _KA.c = None
            if attempt:
                break
    raise SvcError("svc unreachable: " + str(last))


# ── 与 server.py 同名适配（app.py 只依赖这批） ────────────────────────────────
def ykt(method, path, body=None, timeout=20, acct="main", auth="", app=False, headers_extra=None):
    d = call("/raw", {"method": method, "path": path, "body": body, "timeout": timeout,
                      "acct": acct, "auth": auth, "app": bool(app)}, timeout=timeout + 15)
    return d.get("status", 0), d.get("text", ""), d.get("auth", "")


def list_accounts():
    return call("/accounts/list").get("accounts") or []


def add_account(label, cookie, domain=""):
    return call("/accounts/add", {"label": label, "cookie": cookie, "domain": domain}, 60)


def acct_courses(acct):
    return call("/courses", {"acct": acct}, 90).get("courses") or []


def courses(acct):
    """返回 (rows, groups)；分组在核心服务端完成（同名同学期多班合并）。"""
    d = call("/courses", {"acct": acct}, 90)
    return d.get("courses") or [], d.get("groups") or []


def group_courses(rows):
    """兼容占位：真正的分组由服务端 /courses 下发。"""
    return []


def live_now(acct):
    return call("/live", {"acct": acct}, 90).get("live") or []


def attend_state(acct, cid, lid):
    r = call("/attend", {"acct": acct, "cid": cid, "lid": lid}, 60)
    return r or {}


def timetable(acct, days=42):
    r = call("/timetable", {"acct": acct, "days": days}, 180)
    return r.get("tt") or {"slots": [], "courses": 0}


def live_all(acct):
    return call("/live-all", {"acct": acct}, 60).get("live") or []


def live_lesson(acct, cid):
    return call("/live-lesson", {"acct": acct, "cid": cid}, 90).get("lessonId") or ""


def do_checkin(acct, lid, source=5):
    d = call("/checkin", {"acct": acct, "lid": lid, "source": source}, 60)
    return bool(d.get("ok")), str(d.get("detail") or "")


def geo_flow(acct, lid, lat, lng, address="", gcj=True):
    return call("/geo/flow", {"acct": acct, "lid": lid, "lat": lat, "lng": lng,
                              "address": address, "gcj": gcj}, 120)


def geo_auto(acct, lid, address="", frac=0.45):
    return call("/geo/auto", {"acct": acct, "lid": lid, "address": address,
                              "frac": frac}, 150)


def loc_eligibility(acct, lid, auth=""):
    return call("/geo/eligibility", {"acct": acct, "lid": lid, "auth": auth}, 60)


def watch_start(acct, lid, secs=3600):
    return call("/watch/start", {"acct": acct, "lid": lid, "secs": secs}, 30)


def watch_stop(acct, lid):
    return call("/watch/stop", {"acct": acct, "lid": lid}, 30)


def watch_status(acct, lid):
    return call("/watch/status", {"acct": acct, "lid": lid}, 30)


def replay(raw, acct="main"):
    return call("/replay", {"raw": raw, "acct": acct}, 60)


def scan_exchange(raw, accts=None, source=21):
    return call("/scan-exchange", {"raw": raw, "accts": accts or [], "source": source}, 120)


def pool_add(raw, by=""):
    return call("/pool/add", {"raw": raw, "by": by}, 30)


def pool_status():
    return call("/pool/status", {}, 30)


def pool_try(force=False, accts=None, source=21):
    return call("/pool/try", {"force": bool(force), "accts": accts or [], "source": source}, 180)


def pool_drop(all_=False, raw=""):
    return call("/pool/drop", {"all": bool(all_), "raw": raw}, 30)


def health():
    try:
        with urllib.request.urlopen(URL + "/health", timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"ok": False}
