"""失败告警：一个 webhook 走天下（企业微信 / 钉钉 / 飞书 / Server酱 / Bark / 自建）。

配置 /opt/yktsign/alert.json（AES 信封，同 ai.json）：{"webhook": "https://...", "on": 1}
send() 永不抛异常；同一 key 在 gap 秒内只发一次，避免刷屏。
"""
import json
import os
import time
import urllib.parse
import urllib.request

import secure

F = "/opt/yktsign/alert.json"
_LAST = {}


def _cfg():
    try:
        return secure.read_json(F, {}) or {}
    except Exception:
        return {}


def cfg():
    c = _cfg()
    return {"webhook": str(c.get("webhook") or ""), "on": int(c.get("on", 1) or 0)}


def save(webhook=None, on=None):
    c = _cfg()
    if webhook is not None:
        c["webhook"] = str(webhook).strip()
    if on is not None:
        c["on"] = 1 if str(on) not in ("0", "false", "False", "") else 0
    secure.write_json(F, c)
    return cfg()


def _post(url, data, ctype, timeout=7):
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "ykt-alert/1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def send(title, text, key="", gap=600):
    c = _cfg()
    url = str(c.get("webhook") or "").strip()
    if not url or not int(c.get("on", 1) or 0):
        return False
    if key:
        now = time.time()
        if now - _LAST.get(key, 0) < gap:
            return False
        _LAST[key] = now
        if len(_LAST) > 500:
            _LAST.clear()
    title = str(title or "")[:80]
    text = str(text or "")[:500]
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if any(x in host for x in ("wecom", "qyapi", "dingtalk", "feishu", "larksuite")):
            body = json.dumps({"msgtype": "text", "text": {"content": title + "\n" + text}},
                              ensure_ascii=False).encode()
            return _post(url, body, "application/json") in (200, 204)
        if any(x in host for x in ("sctapi", "ftqq")):
            body = urllib.parse.urlencode({"title": title, "desp": text}).encode()
            return _post(url, body, "application/x-www-form-urlencoded") in (200, 204)
        if any(x in host for x in ("day.app", "bark")):
            body = json.dumps({"title": title, "body": text}, ensure_ascii=False).encode()
            return _post(url, body, "application/json") in (200, 204)
        body = json.dumps({"title": title, "text": text}, ensure_ascii=False).encode()
        return _post(url, body, "application/json") in (200, 204)
    except Exception as exc:
        print("alert fail", repr(exc)[:140], flush=True)
        return False
