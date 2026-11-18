import base64, json, os, socket, ssl, struct, sys, time, urllib.request, urllib.error

sys.path.insert(0, os.environ.get("YKT_HOME") or "/opt/yktsign")
import secure
secure.boot()
import ai

HOST = "changjiang.yuketang.cn"
import re as _re
ACCT = _re.sub(r"[^A-Za-z0-9_.-]", "_", (sys.argv[1] if len(sys.argv) > 1 else "main"))[:32] or "main"
LID = _re.sub(r"[^0-9]", "", (sys.argv[2] if len(sys.argv) > 2 else ""))[:32]
OUT = "/opt/yktsign/events"
os.makedirs(OUT, exist_ok=True)
SECS = int(sys.argv[3]) if len(sys.argv) > 3 else 3600
TOKF = "/opt/yktsign/lesson_auth_" + ACCT + ".json"
ANSF = "/opt/yktsign/answers.jsonl"

def ck_file(a):
    return "/opt/yktsign/cookies.json" if a == "main" else "/opt/yktsign/accounts/" + a + ".json"

def _db_cookie(a):
    """线上账号存在 biz.db（acct.put 的加密 cookie），accounts/*.json 是旧部署遗留。

    以前只读文件：acct 形如 u3_a1 的账号没有对应文件 → CK 为空 → 跟课一律 401。
    这里改成 DB 优先、文件回落。
    """
    try:
        import acct as A
        ck = A.cookie(a)
        if ck:
            return ck
    except Exception:
        pass
    try:
        return secure.read_json(ck_file(a), {}) or {}
    except Exception:
        return {}


_ck = _db_cookie(ACCT)
if isinstance(_ck, str):
    CK = _ck.strip()
else:
    CK = "; ".join(str(k) + "=" + str(v) for k, v in (_ck or {}).items()
                   if not str(k).startswith(("__", "_")))

def req(method, path, auth="", body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request("https://" + HOST + path, method=method, data=data)
    r.add_header("Content-Type", "application/json")
    r.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0) Chrome/123")
    r.add_header("xtbz", "ykt")
    r.add_header("X-Client", "desktop")
    r.add_header("Origin", "file://")
    r.add_header("referer", "https://" + HOST + "/lesson/fullscreen/v3/" + str(LID) + "?source=5")
    r.add_header("Cookie", CK)
    if auth:
        r.add_header("Authorization", "Bearer " + auth)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, {}, repr(exc)

def jwt_exp(tok):
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return int(json.loads(base64.urlsafe_b64decode(seg)).get("exp") or 0)
    except Exception:
        return 0

def cached():
    try:
        d = json.load(open(TOKF, encoding="utf-8"))
    except Exception:
        return None
    if str(d.get("lessonId")) != str(LID):
        return None
    exp = jwt_exp(d.get("lessonToken") or "")
    if exp and exp - time.time() < 300:
        return None
    return d

def get_token():
    c = cached()
    if c:
        return c
    st, hd, body = req("POST", "/api/v3/lesson/checkin", body={"source": 5, "lessonId": LID})
    data = {}
    try:
        data = (json.loads(body) or {}).get("data") or {}
    except Exception:
        pass
    tok = data.get("lessonToken") or ""
    print("checkin", st, body[:150], flush=True)
    if not tok:
        return None
    rec = {"lessonId": str(LID), "lessonToken": tok,
           "identityId": str(data.get("identityId") or ""),
           "setauth": hd.get("Set-Auth") or "", "at": int(time.time())}
    json.dump(rec, open(TOKF, "w", encoding="utf-8"), ensure_ascii=False)
    return rec

EVF = os.path.join(OUT, LID + ".jsonl")

def record(obj):
    obj["_at"] = int(time.time())
    with open(EVF, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def log_answer(obj):
    obj["at"] = int(time.time())
    with open(ANSF, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

PRES = {"id": "", "slides": {}}
DONE = set()

def _seed_done():
    try:
        with open(ANSF, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if str(o.get("lesson")) == str(LID) and o.get("problem"):
                    DONE.add(str(o.get("problem")))
    except OSError:
        pass

_seed_done()

def load_pres(presid, setauth):
    if PRES["id"] == presid and PRES["slides"]:
        return PRES["slides"]
    st, hd, body = req("GET", "/api/v3/lesson/presentation/fetch?presentation_id=" + str(presid), auth=setauth)
    try:
        data = (json.loads(body) or {}).get("data") or {}
    except Exception:
        return {}
    for s in data.get("slides") or []:
        p = s.get("problem")
        if p:
            PRES["slides"][str(p.get("problemId"))] = p
    PRES["id"] = presid
    print("pres loaded", st, "problems", len(PRES["slides"]), flush=True)
    return PRES["slides"]

def submit(pid, ptype, result, setauth):
    # 主观题（type 5）服务端 result 取 JsonObject：string 会 500 "String cannot be cast to JsonObject"
    cands = [result]
    if int(ptype) == 5 and isinstance(result, str):
        # 官方 App 客户端 schema：result = {content, pics:[{pic,thumb}], videos:[]}
        cands = [{"content": result, "pics": [{"pic": "", "thumb": ""}], "videos": []},
                 {"content": result, "pics": []},
                 result]
    last = (0, "")
    for idx, r in enumerate(cands):
        payload = {"problemId": str(pid), "problemType": int(ptype),
                   "dt": int(time.time() * 1000), "result": r}
        st, hd, body = req("POST", "/api/v3/lesson/problem/answer", auth=setauth, body=payload, timeout=8)
        print("SUBMIT", pid, "try", idx, json.dumps(payload)[:160], "->", st, body[:160], flush=True)
        log_answer({"lesson": LID, "problem": pid, "type": ptype, "payload": payload, "status": st, "body": body[:300]})
        last = (st, body)
        if st == 200 and ('"code":0' in (body or "") or "50026" in (body or "")
                          or "50013" in (body or "") or "50004" in (body or "")):
            return st, body
        if "cannot be cast" not in (body or ""):
            return st, body
        time.sleep(0.6)
    return last

def _blank(v):
    """答案是不是空的：空字符串、空列表、全是空白都算空。"""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple)):
        return not [x for x in v if str(x if x is not None else "").strip()]
    return False


def fallback_for(ptype, options):
    """AI 没答出来的兜底：保证一定能交上去，绝不空着。"""
    t = int(ptype or 1)
    if t in (1, 2, 3, 6):
        keys = [str((o or {}).get("key") or "").upper() for o in (options or [])]
        keys = [k for k in keys if k]
        return [keys[0]] if keys else ["A"]
    if t == 4:
        return ["略"]
    return "略"


def ai_try(fn, fallback, tries=2):
    """AI 偶发超时/限流时重试一次，仍失败才用兜底。"""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            print("AI FAIL", i, repr(exc)[:140], flush=True)
            time.sleep(1.2)
    return fallback


def handle_problem(pid, setauth, presid, limit=0):
    prob = load_pres(presid, setauth).get(str(pid))
    if not prob:
        print("problem meta missing", pid, flush=True)
        return
    ptype = int(prob.get("problemType") or 1)
    options = prob.get("options") or []
    body = prob.get("body") or ""
    fb = fallback_for(ptype, options)
    if ptype in (1, 2, 3, 6) and options:
        result = ai_try(lambda: ai.answer_options(body, options, ptype), None)
        if _blank(result):
            result = fb
    elif ptype == 4:
        nb = max(1, len(prob.get("blanks") or [""]))
        result = []
        for k in range(nb):
            qk = body if nb == 1 else (body + "\n（只回答第 " + str(k + 1) + " 空，别写题号）")
            result.append(ai_try(lambda q=qk: ai.answer_short(q), "") or "")
        if _blank(result):
            result = fb
        elif any(_blank(x) for x in result):
            result = [x if not _blank(x) else fb[0] for x in result]
    else:
        result = ai_try(lambda: ai.answer_short(body), "")
        if _blank(result):
            result = fb
    print("ANSWER", pid, "type", ptype, "picked", result, flush=True)
    if limit and limit > 12:
        wait = min(limit - 8, 240)
        if wait > 0:
            time.sleep(wait)
    ok = False
    used_fb = False
    while True:
        for _ in range(3):
            st, resp = submit(pid, ptype, result, setauth)
            if '"code":0' in (resp or ""):
                ok = True
                break
            if "50026" in (resp or "") or "50013" in (resp or ""):
                ok = True           # 已交过/已结束：视为该题了结，不再重试
                break
            time.sleep(2)
        if ok or used_fb:
            break
        # AI 答案一轮都没交上去：换成兜底答案再交一次，宁可答错也不能空着
        fb2 = fallback_for(ptype, options)
        if fb2 == result:
            break
        print("ANSWER fallback", pid, "->", fb2, flush=True)
        result = fb2
        used_fb = True
    if ok:
        DONE.add(str(pid))          # 只有提交成功才记完成，失败下次还能补
    else:
        print("ANSWER GIVEUP", pid, "type", ptype, flush=True)

class WS:
    def __init__(self, sock, buf):
        self.sock = sock
        self.buf = buf
    def need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk

    def _frame(self):
        # 整帧补齐后再切：异常路径不改 self.buf，避免半帧把后续帧错位
        self._fill(2)
        b0 = self.buf[0]
        b2 = self.buf[1]
        ln = b2 & 0x7F
        hdr = 2 if ln < 126 else (4 if ln == 126 else 10)
        masked = bool(b2 & 0x80)
        mlen = 4 if masked else 0
        self._fill(hdr + mlen)
        if ln == 126:
            ln = struct.unpack("!H", self.buf[2:4])[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self.buf[2:10])[0]
        total = hdr + mlen + ln
        self._fill(total)
        frame, self.buf = self.buf[:total], self.buf[total:]
        payload = frame[hdr + mlen:hdr + mlen + ln]
        if masked:
            mk = frame[hdr:hdr + 4]
            payload = bytes(payload[i] ^ mk[i % 4] for i in range(len(payload)))
        return b0 & 0x0F, payload, bool(b0 & 0x80)

    def read(self):
        # 服务端大帧会分片（FIN=0），必须把后续 continuation 帧拼起来，否则 JSON 被截断
        op, payload, fin = self._frame()
        guard = 0
        while not fin and guard < 64:
            guard += 1
            op2, p2, fin = self._frame()
            payload += p2
            if op2 == 0x8:
                break
        return op, payload

def mask_write(sock, opcode, payload):
    mask = os.urandom(4)
    ln = len(payload)
    if ln < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | ln)
    elif ln < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, ln)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, ln)
    sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

def connect(path):
    raw = socket.create_connection((HOST, 443), timeout=15)
    sck = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    line = ("GET " + path + " HTTP/1.1\r\nHost: " + HOST + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\nOrigin: https://" + HOST + "\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0) Chrome/123\r\nCookie: " + CK + "\r\n\r\n")
    sck.sendall(line.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sck.recv(4096)
        if not chunk:
            raise RuntimeError("closed")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    first = head.decode("latin1").splitlines()[0]
    if "101" not in first:
        raise RuntimeError(first)
    return WS(sck, rest)

_ALERTED = {}


def _alert_rollcall(nm):
    """老师随机点名。ws 帧 op=callpaused，顶层 name 是被点到的学生姓名。

    雨课堂没有学生端应答/代答接口 —— thuhollow2/yuketang、zhangchi2004/THU-Yuketang-Helper、
    linfish330/ykt_signin 三个公开项目收到这一帧也只是提醒，故这里只做秒级告知：写 notices + 日志。
    """
    nm = str(nm or "").strip()
    if not nm:
        return False
    if time.time() - (_ALERTED.get(nm) or 0) < 90:
        return False
    _ALERTED[nm] = time.time()
    mine, uid = "", 0
    try:
        import sqlite3 as _s3
        cn = _s3.connect("/opt/yktsign/data/biz.db", timeout=5)
        row = cn.execute("SELECT owner_id, name FROM ykt_accounts WHERE acct=?", (ACCT,)).fetchone()
        cn.close()
        if row:
            uid, mine = int(row[0] or 0), str(row[1] or "")
    except Exception as exc:
        print("rollcall db error", repr(exc)[:120], flush=True)
    hit = bool(mine) and (nm == mine or nm in mine or mine in nm)
    print("ROLLCALL", ("ME:" if hit else "") + nm, "at", time.strftime("%H:%M:%S"), flush=True)
    try:
        import sqlite3 as _s3
        cn = _s3.connect("/opt/yktsign/data/biz.db", timeout=5)
        cn.execute("INSERT INTO notices(user_id,kind,title,body,ref,is_read,at) VALUES(?,?,?,?,?,0,?)",
                   (uid, "rollcall",
                    ("点名点到你了：" + nm + "，赶紧响应老师") if hit else ("老师点了" + nm + "（不是你，不用答）"),
                    "这是随机点名，被点到的同学要回应老师（雨课堂没有代答接口，只能提醒）。",
                    ACCT + ":" + LID, int(time.time())))
        cn.commit()
        cn.close()
    except Exception as exc:
        print("rollcall notice error", repr(exc)[:120], flush=True)
    return hit

end = time.time() + SECS
backoff = 3
while time.time() < end:
    try:
        tk = get_token()
        if not tk:
            time.sleep(8)
            continue
        ws = connect("/wsapp/")
        mask_write(ws.sock, 0x1, json.dumps({"op": "hello", "userid": tk["identityId"], "role": "student",
                                             "auth": tk["lessonToken"], "lessonid": LID}).encode())
        ws.sock.settimeout(5)
        print("ws connected", time.strftime("%H:%M:%S"), flush=True)
        last_ping = time.time()
        backoff = 3
        presid = PRES["id"]
        while time.time() < end:
            if time.time() - last_ping > 12:
                mask_write(ws.sock, 0x9, b"keep")
                last_ping = time.time()
            try:
                op, payload = ws.read()
            except socket.timeout:
                continue
            if op == 0x8:
                print("server close", time.strftime("%H:%M:%S"), flush=True)
                break
            if op == 0x9:
                # 服务器 ping 必须回 pong，不回会被判半开连接直接 RST（日志里的 loop error closed）
                try:
                    mask_write(ws.sock, 0xA, payload)
                except Exception:
                    raise RuntimeError("closed")
                continue
            if op == 0xA:
                continue
            if op != 0x1:
                continue
            text = payload.decode("utf-8", "replace")
            try:
                obj = json.loads(text)
            except Exception:
                obj = {"raw": text[:400]}
            record(obj)
            o = obj.get("op")
            if obj.get("presentation"):
                presid = str(obj.get("presentation"))
            print("FRAME", o, text[:170], flush=True)
            if o == "unlockproblem":
                setauth = tk.get("setauth") or ""
                ids = obj.get("problems") or obj.get("problemids") or []
                limit = int(obj.get("limit") or 0)
                for pid in ids:
                    handle_problem(pid, setauth, presid, limit)
            if o in ("showpresentation", "presentationupdated"):
                np_ = obj.get("presentation")
                if np_ and str(np_) != str(PRES["id"]):
                    presid = str(np_)
                    PRES["id"] = ""
                    PRES["slides"] = {}
            if o in ("hello", "slidenav", "slide"):
                sl = obj.get("slide") or {}
                if sl.get("pres"):
                    presid = str(sl.get("pres"))
                if obj.get("presentation"):
                    presid = str(obj.get("presentation"))
                ids = obj.get("unlockedproblem") or []
                if ids:
                    setauth = tk.get("setauth") or ""
                    for pid in ids:
                        if str(pid) in DONE:
                            continue
                        handle_problem(pid, setauth, presid, 0)
            if o == "callpaused":
                _nm = obj.get("name") or ""
                if not _nm and isinstance(obj.get("data"), dict):
                    _nm = (obj.get("data") or {}).get("name") or ""
                _alert_rollcall(_nm)
            if o == "lessonfinished":
                print("lesson finished", flush=True)
                end = 0
                break
    except Exception as exc:
        print("loop error", repr(exc), flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
