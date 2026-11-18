"""secure.py — 凭据加密存储 + 完整性校验/反注入。

1) 落盘加密：cookies.json / accounts/*.json / lesson_auth*.json / ai.json 用
   AES-256-GCM 信封存储（key 在 secret.key，0600，与数据同机不同文件）。
2) 完整性校验：PR_SET_DUMPABLE=0（禁 ptrace/core）、TracerPid 巡检、
   frida/gdb/ld.so.preload 注入面检测、调试器模块检测；命中即静默退出。
3) 监听守护：3 秒一轮复检，注入发生在运行期也能立刻退出。
"""
import base64
import ctypes
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KEYF = os.path.join(BASE, "secret.key")
ENV = "enc.v1"
_guard_on = False


# ── key ──────────────────────────────────────────────────────────────────────
_KEY = None


def key():
    """读主密钥：进程内缓存；已有文件绝不覆盖（覆盖 = 全库密文永久不可解）。"""
    global _KEY
    if _KEY:
        return _KEY
    try:
        raw = open(KEYF, "rb").read()
    except OSError:
        raw = b""
    if len(raw) >= 32:
        _KEY = raw[:32]
        return _KEY
    # 只有「文件不存在 / 空文件」才创建；已存在但过短说明损坏，报错等人恢复备份
    raw = os.urandom(32)
    try:
        fd = os.open(KEYF, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        old = open(KEYF, "rb").read()
        if len(old) >= 32:
            _KEY = old[:32]
            return _KEY
        raise RuntimeError("secret.key 损坏(len=%d)，拒绝自动换钥，请从备份恢复" % len(old))
    os.write(fd, raw)
    os.fsync(fd)
    os.close(fd)
    try:
        os.chmod(KEYF, 0o600)
    except OSError:
        pass
    _KEY = raw
    return _KEY


def _aes():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key())


# ── 信封读写 ────────────────────────────────────────────────────────────────
def seal(obj):
    nonce = os.urandom(12)
    ct = _aes().encrypt(nonce, json.dumps(obj, ensure_ascii=False).encode(), ENV.encode())
    return {"_enc": ENV, "n": base64.b64encode(nonce).decode(),
            "d": base64.b64encode(ct).decode(), "at": int(time.time())}


def unseal(env):
    try:
        nonce = base64.b64decode(env["n"])
        ct = base64.b64decode(env["d"])
        raw = _aes().decrypt(nonce, ct, ENV.encode())
        return json.loads(raw.decode())
    except Exception:
        return None


def is_sealed(path):
    try:
        obj = json.loads(open(path, "rb").read() or b"{}")
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("_enc") == ENV


def read_json(path, default=None):
    try:
        raw = open(path, "rb").read()
    except OSError:
        return default
    try:
        obj = json.loads(raw or b"{}")
    except Exception:
        return default
    if isinstance(obj, dict) and obj.get("_enc") == ENV:
        val = unseal(obj)
        return default if val is None else val
    return obj if obj is not None else default


def write_json(path, obj):
    """原子写入 + 0600；自动套信封。"""
    data = json.dumps(seal(obj), ensure_ascii=False).encode()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, data)
    os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def migrate(path):
    """把明文 JSON 原地加密（幂等）。"""
    try:
        if not os.path.exists(path) or is_sealed(path):
            return False
        obj = json.loads(open(path, "rb").read() or b"{}")
    except Exception:
        return False
    write_json(path, obj)
    return True


def migrate_all():
    n = 0
    for name in ("cookies.json", "ai.json", "lesson_auth.json"):
        n += 1 if migrate(os.path.join(BASE, name)) else 0
    d = os.path.join(BASE, "accounts")
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                n += 1 if migrate(os.path.join(d, fn)) else 0
    for fn in os.listdir(BASE):
        if fn.startswith("lesson_auth_") and fn.endswith(".json"):
            n += 1 if migrate(os.path.join(BASE, fn)) else 0
    return n


# ── 完整性校验 ──────────────────────────────────────────────────────────────────
BAD_MAPS = ("frida-agent", "frida-gadget", "libfrida", "peers.so", "gum-js-loop",
            "gdbserver", "lldb-server", "pydevd", ".pth_loader")
BAD_MODULES = ("pydevd", "pydevd_frame_eval", "pdb", "bdb", "frida", "ptrace", "gdbserver")
BAD_ENV = ("PYTHONINSPECT", "PYTHONSTARTUP", "PYTHONBREAKPOINT", "YPDB", "PYDEVD",
           "LD_PRELOAD", "LD_AUDIT")


def _prctl_no_dump():
    """PR_SET_DUMPABLE(4)=0：禁止 ptrace attach、禁 core dump、/proc/pid/* 收紧。"""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)
    except Exception:
        pass
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def _traced():
    try:
        for line in open("/proc/self/status", encoding="utf-8"):
            if line.startswith("TracerPid:"):
                return int(line.split(":")[1].strip() or 0) != 0
    except Exception:
        pass
    return False


def _injected():
    try:
        maps = open("/proc/self/maps", encoding="utf-8").read()
    except Exception:
        return False
    return any(t in maps for t in BAD_MAPS)


def check():
    bad = False
    if _traced():
        bad = True
    if _injected():
        bad = True
    # sys.gettrace() 是线程局部的；守护线程里永远是 None，所以扫所有线程的 f_trace
    try:
        if any(getattr(f, "f_trace", None) is not None for f in sys._current_frames().values()):
            bad = True
    except Exception:
        pass
    if any(m in sys.modules for m in BAD_MODULES):
        bad = True
    if any(os.environ.get(k) for k in BAD_ENV):
        bad = True
    return bad


def suicide(code=66):
    try:
        sys.stderr.write("E: guard\n")
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def _lock_tracing():
    """把 Python 级调试入口焊死：任何人调用 settrace/setprofile/breakpoint 即自杀。"""
    try:
        sys.settrace = lambda *a, **k: suicide()
        sys.setprofile = lambda *a, **k: suicide()
        sys.breakpointhook = lambda *a, **k: suicide()
    except Exception:
        pass


def harden(block=True):
    """进程启动即调用。block=True 命中即退出，否则只返回判定。"""
    _prctl_no_dump()
    _lock_tracing()
    if check():
        if block:
            suicide()
        return False
    return True


def guard(interval=3.0):
    """后台守护：运行期被 attach/注入也立刻退出。"""
    global _guard_on
    if _guard_on:
        return
    _guard_on = True

    def loop():
        while True:
            if check():
                suicide()
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


MANIFEST = "/opt/yktsign-build/manifest.sha256"


def verify_manifest(live="/opt/yktsign"):
    """发行完整性：pyc 被改/被换 → 拒绝启动。清单缺失则跳过（首次部署前）。"""
    try:
        import hashlib
        with open(MANIFEST, encoding="utf-8") as f:
            rows = [ln.split() for ln in f if ln.strip()]
    except Exception:
        return True
    bad = []
    for h, name in rows:
        p = os.path.join(live, os.path.basename(name))
        try:
            with open(p, "rb") as f:
                d = hashlib.sha256(f.read()).hexdigest()
        except Exception:
            bad.append(name)
            continue
        if d != h:
            bad.append(name)
    if bad:
        try:
            sys.stderr.write("E: integrity " + ",".join(bad) + "\n")
        except Exception:
            pass
        return False
    return True


def boot():
    harden(block=True)
    if not verify_manifest():
        suicide()
    guard()
    return True
