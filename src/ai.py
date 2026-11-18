import json, os, urllib.request, urllib.error

CFG = "/opt/yktsign/ai.json"
DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"

def cfg():
    try:
        import secure
        return secure.read_json(CFG, {}) or {}
    except Exception:
        return {}

def key():
    c = cfg()
    return str(c.get("api_key") or os.environ.get("DASHSCOPE_API_KEY") or "").strip()

def complete(messages, model=None, timeout=25, temperature=0.1):
    k = key()
    if not k:
        raise RuntimeError("no api key: write /opt/yktsign/ai.json")
    c = cfg()
    base = str(c.get("base_url") or DEFAULT_BASE).rstrip("/")
    body = {"model": model or c.get("model") or DEFAULT_MODEL, "messages": messages,
            "temperature": temperature, "stream": False}
    r = urllib.request.Request(base + "/chat/completions", method="POST", data=json.dumps(body).encode())
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + k)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

def answer_options(body, options, ptype):
    keys = [o["key"] for o in options]
    lines = ["题目：" + (body or "").strip(), "选项："]
    for o in options:
        lines.append(o["key"] + ". " + str(o.get("value") or "").strip())
    multi = ptype == 2
    rule = "多选，答案可多个字母" if multi else "单选或判断，只有一个字母"
    lines.append("要求：" + rule + "。只输出答案字母，多个用英文逗号分隔，不要解释。")
    msgs = [{"role": "system", "content": "你是严谨的大学课程答题助手，直接给出最可能的正确答案。"},
            {"role": "user", "content": "\n".join(lines)}]
    out = complete(msgs, model=(cfg().get("model_vision") or cfg().get("model"))).strip()
    picked = []
    for ch in out.upper():
        if ch in keys and ch not in picked:
            picked.append(ch)
    if not picked:
        picked = [keys[0]]
    if not multi:
        picked = picked[:1]
    return picked

def answer_short(body):
    msgs = [{"role": "system", "content": "你是严谨的大学课程答题助手，答案尽量简短。"},
            {"role": "user", "content": "题目：" + (body or "") + "\n只输出答案内容，不要解释。"}]
    return complete(msgs, model=(cfg().get("model") or DEFAULT_MODEL)).strip()
