"""loc.py — 位置签到（location-checkin）。动态码兑换与凭据复用已下线（2026-11-16）：

动态码：二维码 URL 里的 s 由服务端密钥签发，离线拼不出来（假签名 → 51202），
唯一入口是拿真码整条 URL 交给 app 端 /api/v3/app/scan 换 lessonId，再普通签到。
必须用移动端头（x-client: app + Android UA），桌面头会被 400 打回。

位置签到：GET  /api/v3/lesson/location-checkin/eligibility  （Bearer <Set-Auth>）
          POST /api/v3/lesson/location-checkin/submit
               {lat,lng,lessonId,source:54,address}   —— 无时间字段
          服务端自己算距离/迟到，坐标按 GCJ-02，WGS-84 直塞华南沿海会偏 400~600m。
"""
import json
import math
import threading
import time
import urllib.parse

import secure
import server

def _x(lat, lng):
    a, ee = 6378245.0, 0.00669342162296594323

    def tlat(x, y):
        r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return r

    def tlng(x, y):
        r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    dlat = tlat(lng - 105.0, lat - 35.0)
    dlng = tlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    m = 1 - ee * math.sin(rad) ** 2
    sq = math.sqrt(m)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (m * sq) * math.pi)
    dlng = (dlng * 180.0) / (a / sq * math.cos(rad) * math.pi)
    return dlat, dlng


def wgs2gcj(lat, lng):
    """WGS-84 → GCJ-02。"""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except Exception:
        return lat, lng
    dlat, dlng = _x(lat_f, lng_f)
    return round(lat_f + dlat, 6), round(lng_f + dlng, 6)


def gcj2wgs(lat, lng):
    """GCJ-02 → WGS-84（一次迭代反解，误差 < 1m）。"""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except Exception:
        return lat, lng
    dlat, dlng = _x(lat_f, lng_f)
    return round(lat_f - dlat, 6), round(lng_f - dlng, 6)


def _auth_for(acct, lid, auth=""):
    if auth:
        return auth
    try:
        return server.cached_token(lid) or ""
    except Exception:
        return ""


def loc_eligibility(acct="main", lid="", auth=""):
    """看这节课的位置规则：中心/半径/模式/迟到窗口。无权限=老师没开位置签到。"""
    auth = _auth_for(acct, lid, auth)
    st, text, _ = server.ykt("GET", "/api/v3/lesson/location-checkin/eligibility",
                             body=None, timeout=15, acct=acct, auth=auth)
    out = {"status": st, "authed": bool(auth), "ok": False, "code": None,
           "cfg": {}, "resp": (text or "")[:400], "at": int(time.time())}
    try:
        js = json.loads(text or "{}") or {}
        out["code"] = js.get("code")
        out["ok"] = js.get("code") == 0
        d = js.get("data")
        out["cfg"] = d if isinstance(d, dict) else {}
    except Exception:
        pass
    return out


def loc_preview(acct="main", lid="", lat=None, lng=None, auth=""):
    """距离预检（不改状态）。"""
    auth = _auth_for(acct, lid, auth)
    try:
        _lat, _lng = float(lat), float(lng)
    except (TypeError, ValueError):
        # 前端 profile 里存的坐标可能是空串/None，以前直接 float() 崩成 500
        return {"status": 0, "ok": False, "code": None, "data": {},
                "resp": "坐标无效：lat/lng 不是数字"}
    st, text, _ = server.ykt("POST", "/api/v3/lesson/location-checkin/preview",
                             {"lat": _lat, "lng": _lng, "lessonId": str(lid)},
                             15, acct, auth=auth)
    out = {"status": st, "ok": False, "code": None, "data": {}, "resp": (text or "")[:400]}
    try:
        js = json.loads(text or "{}") or {}
        out["code"] = js.get("code")
        out["ok"] = js.get("code") == 0
        out["data"] = js.get("data") or {}
    except Exception:
        pass
    return out


def loc_submit(acct="main", lid="", lat=None, lng=None, address="", auth="",
               source=54, gcj=True):
    """位置签到提交：{lat,lng,lessonId,source:54,address}，无时间字段。"""
    auth = _auth_for(acct, lid, auth)
    ilat, ilng = (lat, lng)
    if gcj:
        ilat, ilng = wgs2gcj(lat, lng)
    body = {"lat": float(ilat), "lng": float(ilng), "lessonId": str(lid),
            "source": int(source), "address": address or ""}
    st, text, newauth = server.ykt("POST", "/api/v3/lesson/location-checkin/submit",
                                   body, 25, acct, auth=auth)
    out = {"status": st, "ok": False, "code": None, "body": body, "resp": (text or "")[:400],
           "newauth": bool(newauth), "at": int(time.time())}
    try:
        js = json.loads(text or "{}") or {}
        out["code"] = js.get("code")
        out["ok"] = js.get("code") == 0
        out["data"] = js.get("data") or {}
    except Exception:
        pass
    return out


# ───────────────────────── 位置签到全自动（2026-11-16） ─────────────────────────
# 服务端在 submit 时按「登录设备坐标 vs 签到中心点」算距离，坐标必须落在 radius 内。
# 真人到场用自己坐标；「人不到」用中心点 + 半径内的偏移点（对手站的做法就是地图选点）。

GEO_KEYS = {
    'need': ('needLocationSignIn', 'need_location_sign_in', 'need'),
    'signed': ('signed', 'isSigned', 'is_signed'),
    'mode': ('mode',),
    'lat': ('centerLat', 'center_lat', 'lat', 'latitude'),
    'lng': ('centerLng', 'center_lng', 'lng', 'longitude'),
    'radius': ('radiusM', 'radius_m', 'radius'),
    'start': ('startMs', 'start_ms', 'startTime'),
    'end': ('endMs', 'end_ms', 'endTime'),
    'address': ('address',),
    'late': ('isLate', 'is_late'),
    'late_after': ('lateAfterMs', 'late_after_ms'),
    'session': ('session', 'sessionId'),
    'checkin_time': ('checkInTime', 'check_in_time'),
}


def _pick(d, key):
    for k in GEO_KEYS.get(key, ()):
        if k in d and d[k] not in (None, ''):
            return d[k]
    return None


def cfg_of(el):
    """把 eligibility 的 data 归一化成稳定字段（雨课堂键名有 camel/snake 两套）。"""
    d = (el or {}).get('cfg') or {}
    out = {'raw': d, 'need': False, 'signed': False, 'mode': None, 'lat': None, 'lng': None,
           'radius': None, 'start_at': None, 'end_at': None, 'address': '', 'late': False,
           'late_after': None, 'session': None, 'checkin_time': None, 'code': (el or {}).get('code')}
    if not isinstance(d, dict):
        return out
    out['need'] = bool(_pick(d, 'need'))
    out['signed'] = bool(_pick(d, 'signed'))
    out['mode'] = _pick(d, 'mode')
    out['lat'] = _pick(d, 'lat')
    out['lng'] = _pick(d, 'lng')
    out['radius'] = _pick(d, 'radius')
    out['start_at'] = _pick(d, 'start')
    out['end_at'] = _pick(d, 'end')
    out['address'] = _pick(d, 'address') or ''
    out['late'] = bool(_pick(d, 'late'))
    out['late_after'] = _pick(d, 'late_after')
    out['session'] = _pick(d, 'session')
    out['checkin_time'] = _pick(d, 'checkin_time')
    # 实测：开了位置签到时中心点/半径/时间窗都在 session 子对象里，顶层没有这些键。
    # 不展开会导致 stage=no_center（明明有中心点却判成取不到），自动定位整条链失效。
    sess = out['session']
    if isinstance(sess, dict):
        if out['lat'] is None:
            out['lat'] = _pick(sess, 'lat')
        if out['lng'] is None:
            out['lng'] = _pick(sess, 'lng')
        if out['radius'] is None:
            out['radius'] = _pick(sess, 'radius')
        if out['start_at'] is None:
            out['start_at'] = _pick(sess, 'start')
        if out['end_at'] is None:
            out['end_at'] = _pick(sess, 'end')
        if out['late_after'] is None:
            out['late_after'] = _pick(sess, 'late_after')
        if not out['address']:
            out['address'] = sess.get('locationName') or sess.get('address') or ''
    return out


def auto_point(el, frac=0.45, bearing=30.0):
    """中心点 + 半径内偏移，返回 (gcj_lat, gcj_lng, meters)。中心/半径缺失时返回 None。

    eligibility 的 centerLat/centerLng 本身就是 GCJ-02（服务端坐标系），直接偏移即可。
    frac 取 0.45 是留余量：既能明确落在圈内，又不会被判「刚好在边界」。
    """
    c = cfg_of(el) if not isinstance(el, dict) or 'need' not in el else el
    try:
        lat = float(c.get('lat'))
        lng = float(c.get('lng'))
    except Exception:
        return None
    try:
        rad = float(c.get('radius') or 0)
    except Exception:
        rad = 0.0
    if rad <= 0:
        rad = 100.0
    d = max(3.0, min(rad * float(frac), rad - 1.0))
    br = math.radians(bearing)
    dlat = (d * math.cos(br)) / 111320.0
    dlng = (d * math.sin(br)) / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return round(lat + dlat, 6), round(lng + dlng, 6), round(d, 1)


def _grab_auth(acct, lid, auth=''):
    """拿 lesson 级 Set-Auth：先缓存/入参，没有就打一次 source=46 签到（失败也会回 Set-Auth）。"""
    a = _auth_for(acct, lid, auth)
    if a:
        return a, None
    try:
        st, text, sa = server.ykt('POST', '/api/v3/lesson/checkin',
                                  {'source': 46, 'lessonId': str(lid)}, 20, acct)
        if sa:
            name = ('lesson_auth.json' if acct in ('', 'main', None)
                    else ('lesson_auth_' + str(acct) + '.json'))
            try:
                secure.write_json(server.os.path.join(server.BASE, name),
                                  {'lessonId': str(lid), 'auth': sa, 'at': int(time.time())})
            except Exception:
                pass
        return sa or '', {'status': st, 'resp': (text or '')[:200]}
    except Exception as e:
        return '', {'err': str(e)[:120]}


def _lid_of(raw=""):
    """从扫码 URL / 查询串 / 纯数字里取 lessonId。"""
    import re as _re
    import urllib.parse as _up
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        u = _up.urlparse(s if ("://" in s or "?" in s) else "?" + s)
        q = {k: v[0] for k, v in _up.parse_qs(u.query).items()}
        for k in ("lessonId", "lesson_id", "lid", "id", "classroomId"):
            if q.get(k):
                return str(q[k]).strip()
    except Exception:
        pass
    m = _re.search(r"(?:lessonId|lesson_id|lid)=(\d{6,})", s)
    if m:
        return m.group(1)
    return s if (s.isdigit() and len(s) >= 6) else ""


def scan_parse(raw="", acct="main"):
    """只解析二维码，不签到。返回 {lid, app, hint}。"""
    out = {"lid": "", "app": {}, "hint": ""}
    raw = str(raw or "").strip()
    lid = _lid_of(raw)
    if lid:
        out["lid"] = lid
        out["hint"] = "url 里直接带 lessonId"
        return out
    if not raw:
        out["hint"] = "空码"
        return out
    try:
        ap = server.app_scan(raw, acct)
        out["app"] = {k: ap.get(k) for k in ("ok", "code", "type", "value", "flow", "status")}
        typ = str(ap.get("type") or "")
        val = str(ap.get("value") or "")
        if ap.get("ok"):
            if typ == "login":
                out["hint"] = "这是登录二维码，不是签到码"
                return out
            if typ in ("checkin", "lesson", "signin") and val.isdigit() and len(val) >= 12:
                out["lid"] = val
                out["hint"] = "app:" + typ
                return out
            out["hint"] = "app:%s value=%s" % (typ or "?", val[:24])
        else:
            out["hint"] = "app code=%s %s" % (ap.get("code"), (ap.get("resp") or "")[:70])
    except Exception as exc:
        out["hint"] = "app err %r" % (exc,)
    try:
        rp = server.replay(raw, acct)
        lid2 = str(rp.get("lessonId") or "")
        if lid2:
            out["lid"] = lid2
            out["hint"] = (out["hint"] + " · replay ok").strip()
        else:
            out["hint"] = (out["hint"] + " · " + str(rp.get("error") or rp.get("hint") or "")).strip()[:160]
    except Exception as exc:
        out["hint"] = (out["hint"] + " replay err %r" % (exc,))[:160]
    return out


def _live_lids(acct):
    """这个人当前正在上的课节集合（一次请求，失败给空集）。"""
    try:
        return {str(x.get("lessonId") or "") for x in (server.live_all(acct) or []) if x.get("live")}
    except Exception:
        return set()


def who_live(lid, accts, workers=8):
    """该课节在直播的账号（本仓库只对本人账号生效，多账号共享不在范围内）。"""
    """只挑出「当前正在上这节课」的账号：逐人查进行中课堂，并发。"""
    accts = [str(a) for a in (accts or []) if a][:1]
    lid = str(lid or "")
    if not lid or not accts:
        return []
    hit = []
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(accts)))) as ex:
            for a, ok in ex.map(lambda x: (x, lid in _live_lids(x)), accts):
                if ok:
                    hit.append(a)
    except Exception:
        return []
    return hit


def scan_broadcast(raw="", accts=None, source=21, only_live=True):
    """把二维码提交给指定账号（本仓库只处理列表中的第一个账号：即本人）。"""
    """解析一次 → 只对「正在上这节课」的账号签到（码池广播）。"""
    accts = [str(a) for a in (accts or []) if a][:1]
    p = scan_parse(raw, accts[0] if accts else "main")
    res = {"lessonId": p["lid"], "hint": p["hint"], "app": p["app"], "per_acct": {},
           "targets": [], "scanned": len(accts)}
    if not p["lid"] or not accts:
        return res
    targets = accts
    if only_live:
        hit = who_live(p["lid"], accts)
        if accts[0] not in hit:
            hit.insert(0, accts[0])
        targets = hit
    res["targets"] = targets
    for a in targets:
        try:
            ok, det = False, ""
            # 先按二维码链接里给出的路径提交，不成功再走常规入口（仅作用于本人账号）
            if raw:
                try:
                    ro = server.replay(raw, a)
                    if ro.get("ok"):
                        ok, det = True, "由二维码链接提交成功"
                except Exception:
                    pass
            if not ok:
                ok, det = server.do_checkin(a, p["lid"], source)
            res["per_acct"][a] = {"ok": bool(ok), "hint": str(det or "")[:140]}
        except Exception as exc:
            res["per_acct"][a] = {"ok": False, "hint": "err %r" % (exc,)}
    return res


def scan_and_checkin(raw="", accts=None, source=21):
    """一次解析二维码后对本人账号提交（只处理列表中的第一个账号）。"""
    """一次取码 → 指定账号签到。"""
    accts = [str(a) for a in (accts or []) if a][:1] or ["main"]
    out = {"lessonId": "", "per_acct": {}, "scan": {}}
    lid = _lid_of(raw)
    hint = ""
    if not lid and raw:
        # app 端 /api/v3/app/scan：桌面 cookie + app 头即可，无需 App 会话
        try:
            ap = server.app_scan(raw, accts[0])
            out["app_scan"] = {k: ap.get(k) for k in ("ok", "code", "type", "value", "flow", "status")}
            typ = str(ap.get("type") or "")
            val = str(ap.get("value") or "")
            if ap.get("ok"):
                if typ == "login":
                    hint = "这是登录二维码，不是签到码"
                elif typ in ("checkin", "lesson", "signin") and val.isdigit() and len(val) >= 12:
                    lid = val
                    hint = "app:" + typ
                else:
                    hint = "app:%s value=%s" % (typ or "?", val[:24])
            else:
                hint = "app code=%s %s" % (ap.get("code"), (ap.get("resp") or "")[:70])
        except Exception as exc:
            hint = "app err %r" % (exc,)
    if not lid:
        try:
            rp = server.replay(raw, accts[0])
            lid = str(rp.get("lessonId") or "")
            if lid:
                hint = (hint + " · replay ok").strip()
            else:
                hint = (hint + " · " + str(rp.get("error") or rp.get("hint") or "")).strip()[:160]
        except Exception as exc:
            hint = (hint + " replay err %r" % (exc,))[:160]
    out["lessonId"] = lid
    out["scan"] = {"hint": hint or "ok", "raw": str(raw)[:120]}
    if not lid:
        return out
    for a in accts:
        try:
            ok, det = server.do_checkin(a, lid, source)
            out["per_acct"][a] = {"ok": bool(ok), "hint": str(det or "")[:140], "code": None}
        except Exception as exc:
            out["per_acct"][a] = {"ok": False, "hint": "err %r" % (exc,), "code": None}
    return out


def geo_auto(acct='main', lid='', auth='', address='', frac=0.45):
    """位置签到全流程：规则 → 自动坐标 → preview → submit（缩小半径重试）。

    返回 {ok, stage, cfg, coords, preview, submit, detail}
    stage: no_rule(本节没开位置签到) / already(已签) / submitted / out_of_range / no_center / error
    """
    out = {'ok': False, 'stage': '', 'acct': acct, 'lid': str(lid), 'cfg': {}, 'coords': {},
           'preview': None, 'submit': None, 'detail': '', 'at': int(time.time())}
    auth, grabinfo = _grab_auth(acct, lid, auth)
    if grabinfo:
        out['auth_grab'] = grabinfo
    el = loc_eligibility(acct, lid, auth)
    if (not el.get("ok")) and (el.get("status") in (401, 403) or el.get("code") in (401, 50000, 100000)):
        # lesson 级 Set-Auth 只有 30 秒有效期，重取一次再打
        auth, g2 = _grab_auth(acct, lid, auth="")
        if g2:
            out["auth_retry"] = g2
        el = loc_eligibility(acct, lid, auth)
    c = cfg_of(el)
    out['cfg'] = {k: v for k, v in c.items() if k != 'raw'}
    out['cfg_raw'] = c.get('raw')
    if not el.get('ok'):
        # 401/50000 = 拿不到课节令牌（动态码窗口开着，或不是本班正式成员）。
        # 这不是位置签到本身失败，单独成 stage，避免污染统计与文案。
        if el.get('status') in (401, 403) or el.get('code') in (401, 50000):
            out['stage'] = 'no_auth'
            out['detail'] = '本节未放行，拿不到签到令牌'
        else:
            out['stage'] = 'error'
            out['detail'] = 'eligibility code=%s %s' % (el.get('code'), (el.get('resp') or '')[:120])
        return out
    if c.get('signed'):
        out['ok'] = True
        out['stage'] = 'already'
        out['detail'] = '该课节已签到'
        return out
    if not c.get('need'):
        out['ok'] = True
        out['stage'] = 'no_rule'
        out['detail'] = '本节未开启位置签到，通用入口签到已足够'
        return out
    if c.get('lat') is None or c.get('lng') is None:
        out['stage'] = 'no_center'
        out['detail'] = '开了位置签到但没有下发中心点，无法自动定位'
        return out
    addr = address or (c.get('address') or '')
    tries = []
    for f in (frac, 0.25, 0.1, 0.02):
        pt = auto_point(c, f)
        if not pt:
            break
        glat, glng, meters = pt
        pv = loc_preview(acct, lid, glat, glng, auth)
        pd = pv.get('data') or {}
        rec = {'frac': f, 'lat': glat, 'lng': glng, 'meters': meters,
               'inRange': pd.get('inRange'), 'active': pd.get('sessionActive'),
               'reason': pd.get('reason'), 'msg': pd.get('message'), 'code': pv.get('code')}
        tries.append(rec)
        if pd.get('sessionActive') is False:
            out['preview'] = rec
            out['stage'] = 'window_closed'
            out['detail'] = str(pd.get('message') or '位置签到未开始或已失效')
            out['tries'] = tries
            return out
        if (not pv.get('ok')) or pd.get('inRange') is False:
            continue                # preview 未成功或不在范围：换点重试，绝不盲提交
        sb = loc_submit(acct, lid, glat, glng, addr, auth, 54, False)
        out['submit'] = sb
        out['coords'] = {'lat': glat, 'lng': glng, 'meters': meters, 'frac': f, 'gcj': True}
        out['preview'] = rec
        out['tries'] = tries
        if sb.get('ok'):
            out['ok'] = True
            out['stage'] = 'submitted'
            out['detail'] = '位置签到成功 ' + str(sb.get('data') or '')[:120]
        else:
            out['stage'] = 'rejected'
            out['detail'] = 'code=%s %s' % (sb.get('code'), (sb.get('resp') or '')[:140])
        return out
    out['tries'] = tries
    out['stage'] = 'out_of_range'
    out['detail'] = '所有候选坐标都判不在范围：' + str((tries[-1] if tries else {}))[:160]
    return out


def lesson_meta(lid, accts=None):
    """这个 lessonId 属于哪门课：cid / 课程名 / 班级。在上课的查 live，否则从课表反查。"""
    lid = str(lid or "")
    accts = [str(a) for a in (accts or []) if a][:1] or ["main"]
    empty = {"cid": "", "title": "", "cls": ""}
    if not lid:
        return empty
    for a in accts:
        try:
            for it in (server.live_all(a) or []):
                if str(it.get("lessonId") or "") == lid:
                    return {"cid": str(it.get("cid") or ""), "title": str(it.get("title") or ""),
                            "cls": str(it.get("cls") or "")}
        except Exception:
            pass
    for a in accts:
        try:
            tt = server.timetable(a) or {}
            for sl in (tt.get("slots") or []):
                for ls in (sl.get("lessons") or []):
                    if str(ls.get("lessonId") or "") == lid:
                        return {"cid": str(sl.get("cid") or ""), "title": str(sl.get("subject") or ""),
                                "cls": str(sl.get("label") or "")}
        except Exception:
            pass
    return empty


def same_course(acct, lid, cid=""):
    """这个账号是不是在上同一节课 / 同一门课。返回 (是否, 依据)。"""
    lid = str(lid or "")
    cid = str(cid or "")
    try:
        for it in (server.live_all(acct) or []):
            if lid and str(it.get("lessonId") or "") == lid:
                return True, "同一节课正在上"
            if cid and str(it.get("cid") or "") == cid:
                return True, "同一门课正在上"
    except Exception:
        pass
    if cid:
        try:
            tt = server.timetable(acct) or {}
            for sl in (tt.get("slots") or []):
                if str(sl.get("cid") or "") == cid:
                    return True, "课表里有这门课"
        except Exception:
            pass
    return False, ""
