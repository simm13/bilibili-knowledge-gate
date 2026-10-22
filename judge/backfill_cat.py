#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_cat.py - 用当前 rules.py 重算 verdicts 表的 cat 字段（类目）
用法： python3 backfill_cat.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import classify_category  # noqa: E402

DB = os.environ.get("KB_JUDGE_DB", "/opt/kbjudge/verdicts.db")


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute("select bvid, title, cat from verdicts").fetchall()
    changed = 0
    dist = {}
    for bvid, title, old in rows:
        if not title:
            continue
        new = classify_category(title)
        dist[new] = dist.get(new, 0) + 1
        if new != old:
            cur.execute("update verdicts set cat=? where bvid=?", (new, bvid))
            changed += 1
    con.commit()
    print("rows=%d changed=%d" % (len(rows), changed))
    print("分布:", sorted(dist.items(), key=lambda x: -x[1]))
    con.close()


if __name__ == "__main__":
    main()
