"""acct.py — 雨课堂账号统一入库（SQLite）+ 1 用户 1 账号约束。

旧实现把凭据散落在 cookies.json / accounts/*.json，无法表达归属与配额；
这里把账号、归属、状态集中到 data/biz.db 的 ykt_accounts 表，cookie 仍用
secure.seal 信封加密后存库。
"""
import json
import os
import re
import sqlite3
import threading
import time

import secure

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DBPATH = os.path.join(DATA, "biz.db")
LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ykt_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  acct TEXT UNIQUE NOT NULL,
  owner_id INTEGER,
  name TEXT DEFAULT '',
  school TEXT DEFAULT '',
  ykt_uid TEXT DEFAULT '',
  domain TEXT NOT NULL DEFAULT 'changjiang.yuketang.cn',
  cookie TEXT NOT NULL,
  status INTEGER NOT NULL DEFAULT 0,
  online INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ykt_owner ON ykt_accounts(owner_id);
CREATE TABLE IF NOT EXISTS qr_logins(
  sid TEXT PRIMARY KEY,
  owner_id INTEGER NOT NULL,
  domain TEXT NOT NULL,
  token TEXT,
  state TEXT NOT NULL DEFAULT 'waiting',
  acct TEXT,
  msg TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
"""


def db():
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DBPATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def q(sql, args=(), one=False):
    with LOCK:
        conn = db()
        try:
            cur = conn.execute(sql, args)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
        finally:
            conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def _migrate_multi(conn):
    """旧库把 owner_id 设成 UNIQUE，1 个用户只能有 1 个账号。改成多账号：重建表。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ykt_accounts'").fetchone()
    if not row or "owner_id INTEGER UNIQUE" not in (row[0] or ""):
        return False
    ddl = [
        "ALTER TABLE ykt_accounts RENAME TO ykt_accounts_old",
        """CREATE TABLE ykt_accounts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          acct TEXT UNIQUE NOT NULL,
          owner_id INTEGER,
          name TEXT DEFAULT '',
          school TEXT DEFAULT '',
          ykt_uid TEXT DEFAULT '',
          domain TEXT NOT NULL DEFAULT 'changjiang.yuketang.cn',
          cookie TEXT NOT NULL,
          status INTEGER NOT NULL DEFAULT 0,
          online INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL)""",
        """INSERT INTO ykt_accounts(id,acct,owner_id,name,school,ykt_uid,domain,cookie,status,online,created_at,updated_at)
           SELECT id,acct,owner_id,name,school,ykt_uid,domain,cookie,status,online,created_at,updated_at
           FROM ykt_accounts_old""",
        "DROP TABLE ykt_accounts_old",
    ]
    # 逐句执行且不自动提交：任何一句失败都整体回滚，绝不出现「账号从视图里消失」
    old_iso = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        for sql in ddl:
            conn.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = old_iso



def ensure():
    conn = db()
    try:
        try:
            if _migrate_multi(conn):
                conn.commit()
        except Exception:
            conn.rollback()
        conn.executescript(SCHEMA)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ykt_accounts)").fetchall()]
            if "label" not in cols:
                conn.execute("ALTER TABLE ykt_accounts ADD COLUMN label TEXT DEFAULT ''")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def _row(r, with_cookie=False):
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    if with_cookie:
        try:
            d["cookie"] = secure.unseal(json.loads(d.get("cookie") or "{}")) or {}
        except Exception:
            d["cookie"] = {}
    else:
        d.pop("cookie", None)
    return d


def get(acct):
    return _row(q("SELECT * FROM ykt_accounts WHERE acct=?", (str(acct),), one=True))


def cookie(acct):
    d = _row(q("SELECT * FROM ykt_accounts WHERE acct=?", (str(acct),), one=True), True)
    return (d or {}).get("cookie") or {}


def by_owner(owner_id):
    return _row(q("SELECT * FROM ykt_accounts WHERE owner_id=? ORDER BY id LIMIT 1", (int(owner_id),), one=True))


def all_by_owner(owner_id):
    return [_row(r) for r in q("SELECT * FROM ykt_accounts WHERE owner_id=? ORDER BY id", (int(owner_id),))]


def drop(acct, owner_id):
    """删除账号（只能删自己的）；顺手把它名下的任务停掉。"""
    row = q("SELECT * FROM ykt_accounts WHERE acct=? AND owner_id=?", (str(acct), int(owner_id)), one=True)
    if not row:
        return False, "账号不存在或不属于你"
    q("DELETE FROM ykt_accounts WHERE id=?", (row["id"],))
    return True, ""


def list_all():
    return [_row(r) for r in q("SELECT * FROM ykt_accounts ORDER BY id")]


def new_acct_name(owner_id):
    base = "u" + str(int(owner_id))
    if not q("SELECT id FROM ykt_accounts WHERE acct=?", (base,), one=True):
        return base
    for n in range(2, 200):
        cand = base + "_" + str(n)
        if not q("SELECT id FROM ykt_accounts WHERE acct=?", (cand,), one=True):
            return cand
    return base + "_" + str(int(time.time()))


def put(acct, ck, owner_id=None, name="", school="", ykt_uid="", domain="", status=0):
    """新建或更新账号；owner_id 传 None 表示不改归属。"""
    ensure()
    now = int(time.time())
    env = json.dumps(secure.seal(ck))
    old = q("SELECT id, owner_id FROM ykt_accounts WHERE acct=?", (str(acct),), one=True)
    if old is None:
        q("INSERT INTO ykt_accounts(acct,owner_id,name,school,ykt_uid,domain,cookie,status,online,created_at,updated_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (str(acct), owner_id, name, school, ykt_uid, domain or "changjiang.yuketang.cn",
           env, status, 1 if status == 200 else 0, now, now))
    else:
        q("UPDATE ykt_accounts SET name=?,school=?,ykt_uid=?,domain=COALESCE(NULLIF(?,''),domain),"
          "cookie=?,status=?,online=?,updated_at=? WHERE acct=?",
          (name, school, ykt_uid, domain, env, status, 1 if status == 200 else 0, now, str(acct)))
    return get(acct)


def bind(owner_id, acct):
    """1 用户 1 账号：先释放该用户既有账号，再抢占目标账号。"""
    ensure()
    owner_id = int(owner_id)
    cur = q("SELECT * FROM ykt_accounts WHERE owner_id=?", (owner_id,), one=True)
    if cur and cur["acct"] == acct:
        return True, ""
    other = q("SELECT * FROM ykt_accounts WHERE owner_id=? AND acct<>?", (owner_id, acct), one=True)
    if other:
        return False, "该用户已绑定账号 " + str(other["acct"]) + "，请先解绑"
    row = q("SELECT * FROM ykt_accounts WHERE acct=?", (str(acct),), one=True)
    if not row:
        return False, "账号不存在"
    if row["owner_id"] not in (None, owner_id):
        return False, "该账号已被其他用户绑定"
    q("UPDATE ykt_accounts SET owner_id=?, updated_at=? WHERE acct=?", (owner_id, int(time.time()), str(acct)))
    return True, ""


def unbind(owner_id):
    q("UPDATE ykt_accounts SET owner_id=NULL, updated_at=? WHERE owner_id=?", (int(time.time()), int(owner_id)))
    return True


def delete(acct):
    q("DELETE FROM ykt_accounts WHERE acct=?", (str(acct),))
    return True


def touch(acct, status=0, name=None, school=None, ykt_uid=None):
    row = q("SELECT * FROM ykt_accounts WHERE acct=?", (str(acct),), one=True)
    if not row:
        return
    q("UPDATE ykt_accounts SET status=?,online=?,name=COALESCE(?,name),school=COALESCE(?,school),"
      "ykt_uid=COALESCE(?,ykt_uid),updated_at=? WHERE acct=?",
      (int(status), 1 if int(status) == 200 else 0, name, school, ykt_uid,
       int(time.time()), str(acct)))


def seed():
    """把历史文件与 bound 表迁进库（幂等）。"""
    ensure()
    pairs = []
    main_p = os.path.join(BASE, "cookies.json")
    if os.path.exists(main_p):
        ck = secure.read_json(main_p, {}) or {}
        if ck.get("sessionid"):
            pairs.append(("main", ck))
    adir = os.path.join(BASE, "accounts")
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            if not fn.endswith(".json"):
                continue
            ck = secure.read_json(os.path.join(adir, fn), {}) or {}
            if ck.get("sessionid"):
                pairs.append((fn[:-5], ck))
    for name, ck in pairs:
        if q("SELECT id FROM ykt_accounts WHERE acct=?", (name,), one=True):
            continue
        dom = str(ck.pop("__domain", "") or "changjiang.yuketang.cn")
        put(name, ck, None, "", "", "", dom, 0)
    try:
        rows = q("SELECT user_id, acct FROM bound ORDER BY id")
    except sqlite3.Error:
        rows = []
    for r in rows:
        if q("SELECT id FROM ykt_accounts WHERE acct=?", (str(r["acct"]),), one=True):
            if q("SELECT id FROM ykt_accounts WHERE owner_id=?", (int(r["user_id"]),), one=True) is None:
                q("UPDATE ykt_accounts SET owner_id=? WHERE acct=?", (int(r["user_id"]), str(r["acct"])))
    return len(pairs)
