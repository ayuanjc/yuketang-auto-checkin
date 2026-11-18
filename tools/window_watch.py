#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到响应码时间序列采样器（业务优先版）。

它记录各账号对当前直播课打签到接口的响应码，作为时间序列，供事后划定
「响应变化」的边界。它不是一个被动的窗口测量仪。

已实测（2026-11-17 22:57，数据结构 1821000000000000003）：
  main（本班 role=5，已签）连打 3 次 source=46 -> 0, 0, 0
  u54（role=6 旁听，从未签上）连打 3 次 -> 50070, 50070, 50070
两个序列都稳定，因此都不携带「窗口是否开着」的信息。

必须写清的三件事：
  1. DONE（签上即停）是业务取舍，不是疏忽。代价是该组合之后再无观测。
     实测表明继续打只会拿到同一个 0，所以信息损失接近零；但它确实留下了
     一个空白：已签账号在窗口内会返回什么，我们不知道。
  2. 50070/50004 只意味着「该请求被拒」。本采样器只能声称「未观察到签到成功」，
     限流计数、上游日志、任何服务端持久化副作用均未排除。为此对连续被拒的
     组合做退避：被拒 REJECT_SLOW 次后，间隔拉长到 SLOW_GAP。
  3. 要独立观测窗口边界，需要一个「本班 role=5 且不参与自动签到」的观察账号。
     当前本班 role=5 只有 main/u50/u51，全部参与自动签到，暂无干净观察哨。
     在该条件满足之前，本采样器不产出窗口状态真值。

产出 /opt/yktsign/data/window_watch.jsonl
  每行 {"at","acct","lid","cid","code","att","was_ok","chg","first","stop"?}
"""
import sys, json, time, os, sqlite3

sys.path.insert(0, '/opt/yktsign')
import server as SRV

OUT = '/opt/yktsign/data/window_watch.jsonl'
HITS = '/opt/yktsign/data/window_hits.jsonl'
DB = '/opt/yktsign/data/biz.db'
GAP = int(os.environ.get('WW_GAP', '60'))
SLOW_GAP = int(os.environ.get('WW_SLOW', '300'))
REJECT_SLOW = int(os.environ.get('WW_RS', '6'))

LAST = {}
LASTT = {}
ATT = {}
SLOW = set()
DONE = set()


def w(path, d):
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(d, ensure_ascii=False) + '\n')
    except Exception:
        pass


def accts():
    try:
        c = sqlite3.connect(DB)
        rows = c.execute("select distinct acct from tasks where state='active'").fetchall()
        c.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return ['main']


def parse(ok, det):
    d = str(det)
    if '"code":0' in d or ok:
        return 0
    for tok in ('50070', '50004', '50002', '50033', '50000', '401'):
        if tok in d:
            return int(tok)
    return None


def one(acct):
    try:
        lives = SRV.live_all(acct) or []
    except Exception:
        return
    for r in lives[:4]:
        lid = str(r.get('lessonId') or '')
        if not lid:
            continue
        cid = str(r.get('cid') or '')
        k = (acct, lid)
        now = int(time.time())
        if k in DONE:
            continue
        if k in SLOW and (now - LASTT.get(k, 0)) < SLOW_GAP:
            continue
        try:
            ok, det = SRV.do_checkin(acct, lid, source=46, only=True)
        except Exception:
            continue
        code = parse(ok, det)
        LASTT[k] = now
        ATT[k] = ATT.get(k, 0) + 1
        prev = LAST.get(k)
        first = prev is None
        chg = 1 if (prev is not None and prev != code) else 0
        LAST[k] = code
        rec = {'at': now, 'acct': acct, 'lid': lid, 'cid': cid, 'code': code,
               'att': ATT[k], 'was_ok': bool(ok), 'chg': chg}
        if code not in (0, None):
            if ATT[k] >= REJECT_SLOW and k not in SLOW:
                SLOW.add(k)
                rec['slow'] = 1
        if code == 50070:
            rec['det'] = str(det)[:200]
            w(HITS, rec)
            try:
                import biz
                biz.notice_new(1, 'window', '采样命中 50070：%s %s（%s）第 %d 次' % (acct, lid, cid, ATT[k]))
            except Exception:
                pass
        if code == 0:
            DONE.add(k)
            rec['stop'] = 1
        if first or chg or code == 50070 or code == 0 or rec.get('slow') or now % 600 < GAP:
            rec['first'] = 1 if first else 0
            w(OUT, rec)


def main():
    print('window_watch start gap=%ss slow=%ss rs=%s' % (GAP, SLOW_GAP, REJECT_SLOW), flush=True)
    while True:
        for a in accts():
            try:
                one(a)
            except Exception as exc:
                print('ERR', a, repr(exc), flush=True)
        # 心跳：每轮都写，与"有没有状态变化"无关。巡检靠它判断进程死活。
        try:
            with open(os.path.join(os.path.dirname(OUT), 'window_beat'), 'w') as bf:
                bf.write(str(int(time.time())))
        except Exception:
            pass
        time.sleep(GAP)


if __name__ == '__main__':
    main()
