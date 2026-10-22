#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_audit.py - 把裁决记录导出成 CSV，供人工复核
用法：
  export_audit.py            导出今天
  export_audit.py 2026-10-21 导出指定日期
输出：/opt/kbjudge/audit/audit-YYYY-MM-DD.csv
"""
import csv
import os
import sqlite3
import sys
import time
from collections import Counter

DB = os.environ.get("KB_JUDGE_DB", "/opt/kbjudge/verdicts.db")
OUT_DIR = os.environ.get("KB_AUDIT_DIR", "/opt/kbjudge/audit")


def export(day=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    if day:
        y, m, d = [int(x) for x in day.split("-")]
        lo = int(time.mktime(time.strptime("%04d-%02d-%02d 00:00:00" % (y, m, d), "%Y-%m-%d %H:%M:%S")))
        hi = lo + 86400
    else:
        lo = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 00:00:00", "%Y-%m-%d %H:%M:%S")))
        hi = lo + 86400
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT ts,bvid,knowledge,source,model,reason,title FROM verdicts "
        "WHERE ts>=? AND ts<? AND source!='fallback:nometa' ORDER BY ts ASC", (lo, hi)).fetchall()
    con.close()

    fname = os.path.join(OUT_DIR, "audit-%s.csv" % time.strftime("%Y-%m-%d", time.localtime(lo)))
    with open(fname, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "bvid", "url", "knowledge", "source", "model", "reason", "title", "人工结论"])
        for ts, bvid, k, src, model, reason, title in rows:
            w.writerow([time.strftime("%H:%M:%S", time.localtime(ts)), bvid,
                        "https://www.bilibili.com/video/%s/" % bvid,
                        "放行" if k else "拦截", src, model or "", reason or "", title or "", ""])
    # 网页级裁决单独一份
    try:
        wcon = sqlite3.connect(DB)
        wrows = wcon.execute(
            "SELECT ts,host,knowledge,source,model,reason,title FROM web_verdicts "
            "WHERE ts>=? AND ts<? ORDER BY ts ASC", (lo, hi)).fetchall()
        wcon.close()
    except Exception as e:
        print("[audit] 网页裁决导出失败: %s" % str(e)[:80])
        wrows = []
    wname = os.path.join(OUT_DIR, "web-%s.csv" % time.strftime("%Y-%m-%d", time.localtime(lo)))
    with open(wname, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "host", "url", "knowledge", "source", "model", "reason", "title", "人工结论"])
        for ts, host, k, src2, model, reason, title in wrows:
            w.writerow([time.strftime("%H:%M:%S", time.localtime(ts)), host,
                        "https://%s/" % host, "放行" if k else "拦截", src2, model or "",
                        reason or "", title or "", ""])
    src = Counter(r[3] for r in rows)
    k_cnt = sum(1 for r in rows if r[2])
    print("[audit] %s 共 %d 条：放行 %d / 拦截 %d  来源分布 %s"
          % (os.path.basename(fname), len(rows), k_cnt, len(rows) - k_cnt, dict(src)))
    print("[audit] 文件: %s" % fname)
    if wrows:
        wk = sum(1 for r in wrows if r[2])
        print("[audit] 网页裁决 %d 条：放行 %d / 拦截 %d -> %s"
              % (len(wrows), wk, len(wrows) - wk, os.path.basename(wname)))
    return fname


if __name__ == "__main__":
    export(sys.argv[1] if len(sys.argv) > 1 else None)
