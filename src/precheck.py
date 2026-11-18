#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""早八守护：07:45 体检、08:05 验签。发现问题写 notice 给 user1。"""
import sys, os, time, subprocess, json
sys.path.insert(0, '/opt/yktsign')
LOGP = '/opt/yktsign/data/precheck.log'

def w(msg):
    try:
        with open(LOGP, 'a', encoding='utf-8') as f:
            f.write('%d %s\n' % (int(time.time()), msg))
    except Exception:
        pass

def problem(text):
    w('PROBLEM ' + text)
    try:
        import biz
        biz.notice_new(1, 'precheck', '早八自检：' + text[:300])
    except Exception as exc:
        w('notice failed ' + repr(exc)[:120])

def svc_ok():
    bad = []
    for s in ('ykt-svc', 'ykt-web'):
        st = subprocess.run(['systemctl', 'is-active', s], capture_output=True, text=True).stdout.strip()
        if st != 'active':
            bad.append('%s=%s' % (s, st))
            subprocess.run(['systemctl', 'restart', s], capture_output=True)
    return bad

def mode_morning():
    bad = svc_ok()
    if bad:
        problem('服务异常(已尝试重启) ' + ','.join(bad))
    try:
        import server as SRV, sqlite3
        c = sqlite3.connect('/opt/yktsign/data/biz.db')
        accts = [r[0] for r in c.execute("select distinct acct from tasks where state='active'")]
        dead = []
        for a in accts:
            try:
                st, txt, _ = SRV.ykt('GET', '/api/v3/user/basic-info', None, acct=a, timeout=12)
                if not (st == 200 and '"code":0' in (txt or '')):
                    dead.append(a)
            except Exception:
                dead.append(a)
        if dead:
            problem('cookie 失效：' + ','.join(dead))
        w('morning accounts=%d dead=%d' % (len(accts), len(dead)))
        # 08:00 前后有课却没签到 → 告警
        now = int(time.time())
        live = []
        for a in accts[:6]:
            try:
                for x in (SRV.live_all(a) or []):
                    live.append((a, x.get('title'), x.get('lessonId')))
            except Exception:
                pass
        w('live=%d %s' % (len(live), str(live)[:200]))
        if not live:
            w('no live lesson at 07:45 (正常，课 08:00 才开)')
    except Exception as exc:
        problem('体检异常 ' + repr(exc)[:150])

def mode_verify():
    """08:05：今天有没有新签到记录。"""
    try:
        import sqlite3
        c = sqlite3.connect('/opt/yktsign/data/biz.db')
        now = int(time.time())
        since = now - 3600
        rows = list(c.execute("select acct, ok, detail from runs where at>=? order by at", (since,)))
        okn = [r for r in rows if r[1] == 1]
        w('verify runs=%d ok=%d' % (len(rows), len(okn)))
        if not okn:
            problem('近一小时无成功签到记录，检查 08:00 那节课（%s）' % (str([r[0] for r in rows])[:120]))
    except Exception as exc:
        problem('验签异常 ' + repr(exc)[:150])

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'morning'
    (mode_morning if mode == 'morning' else mode_verify)()
