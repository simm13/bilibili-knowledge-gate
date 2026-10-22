#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
judge.py - B站视频"知识性"裁决服务（127.0.0.1:8090）

裁决链： 规则预筛 -> opencode(LLM) 判定 -> 规则兜底（模型不可用时）
缓存：   SQLite（bvid -> 结论/理由/来源），支持批量查询与审计
接口：
  GET  /health                 服务存活
  GET  /verdict?bvid=BVxxx     查裁决（只读缓存，毫秒级）
  GET  /verdicts?bvid=a&bvid=b 批量查
  GET  /judge?bvid=BVxxx       触发裁决（入队，立即返回）
  GET  /stats                  统计
  GET  /audit?limit=50         最近裁决记录
"""
import hashlib
import json
import os
import queue
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import classify_text, classify_category, cat_priority, KNOWLEDGE_WEAK  # noqa: E402
import web_rules  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("KB_JUDGE_PORT", "8090"))
DB_PATH = os.environ.get("KB_JUDGE_DB", os.path.join(
    os.environ.get("KB_HOME", "/opt/kbjudge"), "verdicts.db"))
OC_BASE = os.environ.get("KB_OC_URL", "http://127.0.0.1:4096").rstrip("/")
MODELS = [m.strip() for m in os.environ.get(
    "KB_MODELS",
    "opencode/deepseek-v4.1-flash-free,opencode/glm-5.3-flash-free,"
    "opencode/longcat-2.5-preview-free,opencode/nemotron-3.5-lightning-free",
).split(",") if m.strip()]
WEB_MODELS = [m.strip() for m in os.environ.get(
    "KB_WEB_MODELS",
    "opencode/glm-5.3-flash-free,opencode/longcat-2.5-preview-free,"
    "opencode/deepseek-v4.1-flash-free",
).split(",") if m.strip()]
LLM_TIMEOUT = float(os.environ.get("KB_LLM_TIMEOUT", "25"))
LLM_DEADLINE = float(os.environ.get("KB_LLM_DEADLINE", "70"))
META_TIMEOUT = float(os.environ.get("KB_META_TIMEOUT", "10"))
WORKERS = int(os.environ.get("KB_WORKERS", "2"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/147.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 存储
_db_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("""CREATE TABLE IF NOT EXISTS verdicts(
            bvid TEXT PRIMARY KEY, knowledge INTEGER, reason TEXT, source TEXT,
            model TEXT, title TEXT, ts INTEGER)""")
        try:
            _db.execute("ALTER TABLE verdicts ADD COLUMN aid INTEGER")
        except Exception:
            pass
        try:
            _db.execute("ALTER TABLE verdicts ADD COLUMN cat TEXT")
        except Exception:
            pass
        _db.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_aid ON verdicts(aid)")
        _db.execute("""CREATE TABLE IF NOT EXISTS pending(
            bvid TEXT PRIMARY KEY, title TEXT, reason TEXT, ts INTEGER)""")
        _db.execute("""CREATE TABLE IF NOT EXISTS web_verdicts(
            host TEXT PRIMARY KEY, knowledge INTEGER, reason TEXT, source TEXT,
            model TEXT, title TEXT, ts INTEGER, hits INTEGER DEFAULT 0)""")
        _db.execute("""CREATE TABLE IF NOT EXISTS live_verdicts(
            room_id TEXT PRIMARY KEY, knowledge INTEGER, reason TEXT, source TEXT,
            model TEXT, title TEXT, ts INTEGER)""")
        _db.execute("""CREATE TABLE IF NOT EXISTS pgc_verdicts(
            key TEXT PRIMARY KEY, knowledge INTEGER, reason TEXT, source TEXT,
            model TEXT, title TEXT, ts INTEGER)""")
        _db.commit()
    return _db


def get_verdict(bvid):
    with _db_lock:
        row = db().execute(
            "SELECT knowledge,reason,source,model,title,ts,aid,cat FROM verdicts WHERE bvid=?",
            (bvid,)).fetchone()
    if not row:
        return None
    return {"bvid": bvid, "known": True, "knowledge": bool(row[0]), "reason": row[1],
            "source": row[2], "model": row[3], "title": row[4], "ts": row[5], "aid": row[6],
            "cat": row[7] or ""}


def get_verdict_by_aid(aid):
    """播放流接口常用 avid= 参数，这里按 aid 反查裁决"""
    try:
        aid = int(aid)
    except Exception:
        return None
    with _db_lock:
        row = db().execute(
            "SELECT bvid,knowledge,reason,source,model,title,ts,cat FROM verdicts WHERE aid=?",
            (aid,)).fetchone()
    if not row:
        return None
    return {"bvid": row[0], "known": True, "knowledge": bool(row[1]), "reason": row[2],
            "source": row[3], "model": row[4], "title": row[5], "ts": row[6], "aid": aid,
            "cat": row[7] or ""}


def put_verdict(v):
    with _db_lock:
        db().execute(
            "INSERT OR REPLACE INTO verdicts(bvid,knowledge,reason,source,model,title,ts,aid,cat)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (v["bvid"], 1 if v["knowledge"] else 0, v.get("reason", ""), v.get("source", ""),
             v.get("model", ""), v.get("title", ""), int(time.time()), int(v.get("aid") or 0),
             v.get("cat") or ""))
        db().commit()


def mark_pending(bvid, title, reason):
    with _db_lock:
        db().execute("INSERT OR REPLACE INTO pending(bvid,title,reason,ts) VALUES(?,?,?,?)",
                     (bvid, title or "", reason or "", int(time.time())))
        db().commit()


def clear_pending(bvid):
    with _db_lock:
        db().execute("DELETE FROM pending WHERE bvid=?", (bvid,))
        db().commit()


def pending_list(limit=20):
    with _db_lock:
        rows = db().execute(
            "SELECT bvid,title,reason FROM pending ORDER BY ts ASC LIMIT ?", (limit,)).fetchall()
    return [{"bvid": r[0], "title": r[1], "reason": r[2]} for r in rows]


# ---------------------------------------------------------------- 网页级裁决
def get_web_verdict(host):
    with _db_lock:
        row = db().execute(
            "SELECT knowledge,reason,source,model,title,ts,hits FROM web_verdicts WHERE host=?",
            (host,)).fetchone()
    if not row:
        return None
    return {"host": host, "known": True, "knowledge": bool(row[0]), "reason": row[1],
            "source": row[2], "model": row[3], "title": row[4], "ts": row[5], "hits": row[6]}


def delete_web_verdict(host):
    with _db_lock:
        db().execute("DELETE FROM web_verdicts WHERE host=?", (host,))
        db().commit()


def put_web_verdict(host, knowledge, reason, source, model="", title=""):
    with _db_lock:
        db().execute(
            "INSERT OR REPLACE INTO web_verdicts(host,knowledge,reason,source,model,title,ts,hits)"
            " VALUES(?,?,?,?,?,?,?,COALESCE((SELECT hits FROM web_verdicts WHERE host=?),0)+1)",
            (host, 1 if knowledge else 0, reason, source, model, title, int(time.time()), host))
        db().commit()


WEB_PROMPT = """你是网页分类器。判断给定网页的**主要用途**是否属于"知识/工具类"。

判为知识类（knowledge=true）：
- 百科词典、文档与 API 文档、教程课程、论文学术、代码仓库与技术问答
- 新闻资讯、时政、财经研报、政务与学校官网、在线工具、办公协作系统
- 天气地图、票务出行、查快递、查资料等实用查询

判为非知识类（knowledge=false）：
- 游戏站点/攻略/开黑社区、短视频与直播、影视综艺与二次元
- 社交八卦、论坛灌水与热榜段子、小说漫画、种草购物与消费测评
- 音乐娱乐、成人与博彩、纯娱乐内容聚合站

注意：按网页主要用途判断，不是按"是否有用"。拿不准时判 true（宁可放行）。
只输出一个 JSON 对象，不要任何解释性文字：
{"knowledge": true 或 false, "reason": "不超过12个字的依据"}

域名：%(host)s
标题：%(title)s
描述：%(desc)s
"""


def llm_web_verdict(host, title, desc, deadline):
    prompt = WEB_PROMPT % {"host": host, "title": (title or "")[:120],
                           "desc": (desc or "")[:300]}
    for model in WEB_MODELS:
        if time.time() >= deadline:
            break
        for attempt in range(2):
            if time.time() >= deadline:
                break
            try:
                txt = oc_ask(model, prompt, deadline)
            except Exception as e:
                log("web llm model=%s attempt=%d failed: %s" % (model, attempt, str(e)[:100]))
                time.sleep(0.3)
                continue
            r = parse_verdict(txt)
            if r:
                return (r[0], r[1], "llm:" + model)
            log("web llm model=%s unparsable: %s" % (model, txt[:100]))
    return None


def web_rule_verdict(host, title="", desc=""):
    """域名规则层：allow / block / grey"""
    if web_rules.is_exempt(host):
        return (True, "自有门禁站点，豁免", "rule:web:exempt")
    rt = web_rules.runtime_allow(host)
    if rt:
        return (True, "运行时白名单(%s)" % rt, "rule:web:allow")
    kind, hit = web_rules.lookup(host)
    if kind == "allow":
        return (True, "知识/工具类站点(%s)" % hit, "rule:web:allow")
    if kind == "block":
        return (False, "娱乐/非知识站点(%s)" % hit, "rule:web:block")
    tb = web_rules.title_block(title, desc)
    if tb:
        return (False, "标题含娱乐特征(%s)" % tb, "rule:web:title")
    return None


def _web_llm_and_store(host, title, desc):
    """跑 LLM 并把结论写进缓存（后台线程里也用它）"""
    lv = llm_web_verdict(host, title, desc, time.time() + LLM_DEADLINE)
    if lv:
        put_web_verdict(host, lv[0], lv[1], "llm", lv[2], title)
        log("web verdict %s knowledge=%s model=%s reason=%s"
            % (host, lv[0], lv[2], lv[1]))
    return lv


def llm_web_verdict_bounded(host, title, desc, maxwait):
    """最多等 maxwait 秒；超时返回 None，但后台线程仍会把结论写进缓存"""
    box = {}
    done = threading.Event()

    def run():
        try:
            box["v"] = _web_llm_and_store(host, title, desc)
        except Exception as e:
            log("web llm thread error %s: %s" % (host, str(e)[:120]))
            box["v"] = None
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    done.wait(max(0.1, maxwait))
    return box.get("v")


_web_q = queue.Queue()
_web_inflight = set()
_web_lock = threading.Lock()


def enqueue_web(host, title, desc):
    with _web_lock:
        if host in _web_inflight:
            return False
        _web_inflight.add(host)
    _web_q.put((host, title, desc))
    return True


def _web_worker():
    while True:
        host, title, desc = _web_q.get()
        try:
            if get_web_verdict(host):
                continue
            rv = web_rule_verdict(host)
            if rv:
                put_web_verdict(host, rv[0], rv[1], rv[2], "", title)
                log("web verdict %s knowledge=%s source=%s" % (host, rv[0], rv[2]))
                continue
            lv = _web_llm_and_store(host, title, desc)
            if not lv:
                log("web verdict %s FAILED (llm unavailable) -> 放行" % host)
        except Exception as e:
            log("web worker error %s: %s" % (host, str(e)[:150]))
        finally:
            with _web_lock:
                _web_inflight.discard(host)
            _web_q.task_done()






# ---------------------------------------------------------------- 学科推荐：数学/物理 定向源
# B 站搜索接口需要 WBI 签名
WBI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
STEM_KEYWORDS = ["高中数学", "高中物理", "考研数学", "初中数学", "物理讲解",
                 "高等数学", "初中物理", "微积分", "力学", "电磁学"]
KB_DIR = os.environ.get("KB_HOME", "/opt/kbjudge")
STEM_CACHE_FILE = os.path.join(KB_DIR, "stem_feed.json")
_wbi_state = {"key": "", "ts": 0, "cookie": ""}


def _bili_cookie():
    if _wbi_state["cookie"]:
        return _wbi_state["cookie"]
    try:
        raw = http_get("https://api.bilibili.com/x/frontend/finger/spi", ua=SUBJECT_UA)
        d = (json.loads(raw.decode("utf-8", "replace")).get("data") or {})
        ck = "buvid3=%s; buvid4=%s" % (d.get("b_3") or "", d.get("b_4") or "")
        if d.get("b_3"):
            _wbi_state["cookie"] = ck
        return ck
    except Exception:
        return ""


def _wbi_key():
    if _wbi_state["key"] and (time.time() - _wbi_state["ts"]) < 3600:
        return _wbi_state["key"]
    try:
        raw = http_get("https://api.bilibili.com/x/web-interface/nav", ua=SUBJECT_UA)
        wbi = (json.loads(raw.decode("utf-8", "replace")).get("data") or {}).get("wbi_img") or {}
        img = re.search(r"/([^/]+)\.png", wbi.get("img_url") or "")
        sub = re.search(r"/([^/]+)\.png", wbi.get("sub_url") or "")
        if not (img and sub):
            return ""
        raw_key = img.group(1) + sub.group(1)
        _wbi_state["key"] = "".join(raw_key[i] for i in WBI_MIXIN_TAB)[:32]
        _wbi_state["ts"] = time.time()
    except Exception as e:
        log("wbi key failed: %s" % str(e)[:80])
    return _wbi_state["key"]


def wbi_search(keyword, page=1, order="totalrank"):
    key = _wbi_key()
    if not key:
        return []
    params = {"search_type": "video", "keyword": keyword, "page": page, "order": order}
    params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    url = ("https://api.bilibili.com/x/web-interface/wbi/search/type?"
           + urllib.parse.urlencode(sorted(params.items())))
    try:
        raw = http_get(url, referer="https://search.bilibili.com/", ua=SUBJECT_UA,
                        headers={"Cookie": _bili_cookie()})
        j = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        log("wbi search %r failed: %s" % (keyword, str(e)[:80]))
        return []
    if j.get("code") != 0:
        log("wbi search %r code=%s" % (keyword, j.get("code")))
        return []
    out = []
    for it in ((j.get("data") or {}).get("result") or []):
        title = re.sub(r"<[^>]+>", "", it.get("title") or "")
        bvid = it.get("bvid") or ""
        if not bvid or not title:
            continue
        try:
            play = int(it.get("play") or 0)
        except Exception:
            play = 0
        out.append({"bvid": bvid, "title": title[:60], "up": it.get("author") or "",
                    "play": play, "cat": classify_category(title)})
    return out


def _stem_load_cache():
    try:
        with open(STEM_CACHE_FILE, "r") as f:
            j = json.load(f)
        if isinstance(j, list):
            return j
    except Exception:
        pass
    return []


def _stem_save_cache(items):
    try:
        tmp = STEM_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f)
        os.replace(tmp, STEM_CACHE_FILE)
    except Exception as e:
        log("stem cache save failed: %s" % str(e)[:80])


def _stem_knowledge_ok(title, bvid):
    """未裁决的用规则层兜底判一次知识性，避免把娱乐/游戏混进数理推荐"""
    v = get_verdict(bvid)
    if v and v.get("known"):
        return bool(v.get("knowledge")), v
    r = classify_text(title)
    if r is not None and not r[0]:
        return False, None
    if v is None or not v.get("known"):
        try:
            enqueue(bvid, force=False, prio=False)
        except Exception:
            pass
    return True, v


def fetch_stem_feed(per_kw=6):
    """搜索数学/物理教学视频（比分区榜单更精准）；结果持久化累积，风控时不空窗"""
    out = list(_stem_load_cache())
    seen = set(x.get("bvid") for x in out)
    for kw in STEM_KEYWORDS:
        res = []
        for attempt in range(2):
            try:
                res = wbi_search(kw)
            except Exception as e:
                log("stem search %r err: %s" % (kw, str(e)[:80]))
                res = []
            if res:
                break
            time.sleep(4.0 + attempt * 3.0)
        n = 0
        for it in res:
            if it["bvid"] in seen:
                continue
            if it["cat"] not in ("数学", "物理"):
                continue
            ok, v = _stem_knowledge_ok(it["title"], it["bvid"])
            if not ok:
                continue
            seen.add(it["bvid"])
            it["rid_name"] = it["cat"]
            it["rid"] = 0
            it["view"] = it.get("play") or 0
            it["src"] = "search:" + kw
            it["prio"] = cat_priority(it["cat"])
            out.append(it)
            n += 1
            if n >= per_kw:
                break
        time.sleep(3.0 + random.random() * 2.0)
    out.sort(key=lambda x: (-(x.get("prio") or 0), -(x.get("view") or 0)))
    _stem_save_cache(out[:80])
    return out[:80]


# ---------------------------------------------------------------- 学科推荐流
# B 站分区 id：36=知识 188=科技 201=科学科普 202=社科法律 203=人文历史 204=财经商业
SUBJECT_RIDS = [(36, "知识"), (188, "科技"), (201, "科学科普"), (202, "社科法律"),
                (203, "人文历史"), (204, "财经商业"), (208, "职业技能")]
_subject_feed_cache = {"ts": 0, "items": []}
_subject_feed_lock = threading.Lock()


SUBJECT_FEED_FILE = os.path.join(KB_DIR, "subject_feed.json")
SUBJECT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/147.0.0.0 Safari/537.36")


def _subject_ranking(rid, tries=3):
    """排行榜接口对 UA/频率敏感：多 UA + 重试，-352 视为风控可跳过"""
    url = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=%d&type=all" % rid
    for i in range(tries):
        for ua in (SUBJECT_UA, UA):
            try:
                raw = http_get(url, referer="https://www.bilibili.com/", ua=ua)
                j = json.loads(raw.decode("utf-8", "replace"))
                if j.get("code") == 0:
                    lst = ((j.get("data") or {}).get("list") or [])
                    if lst:
                        return lst
            except Exception:
                pass
            time.sleep(0.6)
        time.sleep(1.2 + i)
    return []


def _subject_load_disk():
    try:
        with open(SUBJECT_FEED_FILE, "r") as f:
            j = json.load(f)
        if isinstance(j, dict) and j.get("items"):
            return j["items"]
    except Exception:
        pass
    return []


def _subject_save_disk(items):
    try:
        tmp = SUBJECT_FEED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": int(time.time()), "items": items}, f)
        os.replace(tmp, SUBJECT_FEED_FILE)
    except Exception as e:
        log("subject feed save failed: %s" % str(e)[:80])


def fetch_subject_feed(limit=24):
    """学科推荐素材：数学/物理搜索源 + 学科分区排行榜，按类目优先级排序"""
    items = []
    seen = set()
    for it in fetch_stem_feed(per_kw=6):
        if len(items) >= limit:
            break
        if it.get("bvid") in seen:
            continue
        seen.add(it.get("bvid"))
        items.append(it)
    for rid, rname in SUBJECT_RIDS:
        lst = _subject_ranking(rid)
        if not lst:
            continue
        for it in lst:
            bvid = it.get("bvid") or ""
            title = it.get("title") or ""
            if not bvid or not title or bvid in seen:
                continue
            v = get_verdict(bvid)
            if v and v.get("known") and not v.get("knowledge"):
                continue          # 已知非知识 -> 不推荐
            seen.add(bvid)
            items.append({"bvid": bvid, "title": title[:60],
                          "up": ((it.get("owner") or {}).get("name")) or "",
                          "rid": rid, "rid_name": rname,
                          "view": it.get("stat", {}).get("view") or 0,
                          "cat": (v or {}).get("cat") or classify_category(title),
                          "known": bool(v and v.get("known"))})
            if len(items) >= limit:
                break
        time.sleep(1.0 + random.random())
    for it in items:
        it["prio"] = cat_priority(it.get("cat"))
    items.sort(key=lambda x: (-(x.get("prio") or 0), -(x.get("view") or 0)))
    if items:
        _subject_save_disk(items)
    return items


_subject_refreshing = {"flag": False}


def _subject_refresh(limit):
    try:
        items = fetch_subject_feed(limit)
        if items:
            with _subject_feed_lock:
                _subject_feed_cache["ts"] = time.time()
                _subject_feed_cache["items"] = items
            _subject_save_disk(items)
            log("subject feed refreshed: %d items" % len(items))
    except Exception as e:
        log("subject feed refresh failed: %s" % str(e)[:100])
    finally:
        _subject_refreshing["flag"] = False


def subject_feed(limit=24, maxage=21600):
    """非阻塞：缓存过期时后台刷新，主线程立即返回旧缓存/磁盘缓存"""
    with _subject_feed_lock:
        items = _subject_feed_cache["items"]
        ts = _subject_feed_cache["ts"]
    if items and (time.time() - ts) < maxage:
        return items[:limit]
    if not _subject_refreshing["flag"]:
        _subject_refreshing["flag"] = True
        threading.Thread(target=_subject_refresh, args=(max(limit, 24),), daemon=True).start()
    if items:
        return items[:limit]
    disk = _subject_load_disk()          # 风控/网络异常时用磁盘缓存兜底
    if disk:
        with _subject_feed_lock:
            _subject_feed_cache["items"] = disk
        return disk[:limit]
    return []


# ---------------------------------------------------------------- 番剧/综艺/影视（PGC）
# B 站 PGC type 映射（实测）：1=番剧/动画  2=影视  3=纪录片  5=电视剧  7=综艺
PGC_DOC_TYPE = 3                       # 纪录片 -> 知识性
PGC_ENTERTAIN_TYPES = (2, 5, 7)        # 影视/电视剧/综艺 -> 娱乐
PGC_ENTERTAIN_STYLES = ("综艺", "真人秀", "脱口秀", "游戏", "电竞", "娱乐", "搞笑",
                        "真人", "音乐", "舞蹈", "美食", "旅行", "综艺衍生")


def get_pgc_verdict(key):
    row = db().execute(
        "SELECT knowledge, reason, source, model, title FROM pgc_verdicts WHERE key=?",
        (str(key),)).fetchone()
    if not row:
        return None
    return {"key": str(key), "known": True, "knowledge": bool(row[0]),
            "reason": row[1] or "", "source": row[2] or "", "model": row[3] or "",
            "title": row[4] or ""}


def put_pgc_verdict(key, knowledge, reason, source, model="", title=""):
    db().execute(
        "INSERT OR REPLACE INTO pgc_verdicts(key, knowledge, reason, source, model, title, ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (str(key), 1 if knowledge else 0, reason, source, model, title, int(time.time())))
    db().commit()


_pgc_alias = {}          # "ep:123" / "ss:456" -> "md:789"


def fetch_pgc_meta(id_type, id_val):
    """抓 PGC（番剧/综艺/影视/纪录片）信息"""
    id_type = (id_type or "ep").lower()
    url = ""
    if id_type == "ep":
        url = "https://api.bilibili.com/pgc/view/web/season?ep_id=" + str(id_val)
    elif id_type in ("ss", "season"):
        url = "https://api.bilibili.com/pgc/view/web/season?season_id=" + str(id_val)
    elif id_type in ("md", "media"):
        url = "https://api.bilibili.com/pgc/view/web/media?media_id=" + str(id_val)
    out = {"id_type": id_type, "id": str(id_val), "title": "", "desc": "", "styles": [],
           "type": 0, "media_id": "", "season_id": "", "ep_title": ""}
    if not url:
        return out
    try:
        raw = http_get(url, referer="https://www.bilibili.com/")
        j = json.loads(raw.decode("utf-8", "replace"))
        if j.get("code") != 0:
            out["error"] = str(j.get("message") or j.get("code"))[:60]
            return out
        r = j.get("result") or {}
        out["title"] = r.get("title") or ""
        out["desc"] = (r.get("evaluate") or r.get("description") or "").strip()
        st = r.get("styles")
        if isinstance(st, str):
            st = [st]
        out["styles"] = [str(x) for x in (st or [])][:12]
        out["type"] = r.get("type") or 0
        out["media_id"] = str(r.get("media_id") or "")
        out["season_id"] = str(r.get("season_id") or "")
        ep = r.get("ep") or {}
        out["ep_title"] = ep.get("title") or ""
        if not out["media_id"] and id_type == "md":
            out["media_id"] = str(id_val)
        if out["media_id"] and id_type in ("ep", "ss"):
            _pgc_alias["%s:%s" % (id_type, str(id_val))] = "md:" + out["media_id"]
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def pgc_rule_verdict(meta):
    """番剧/综艺规则层：纪录片放行；影视/综艺/电视剧拦截；番剧动画交 LLM"""
    styles = [x.lower() for x in (meta.get("styles") or [])]
    title = meta.get("title") or ""
    ttype = meta.get("type") or 0
    for s in styles:
        for w in PGC_ENTERTAIN_STYLES:
            if w.lower() in s:
                return (False, "内容类型为娱乐向(%s)" % s, "rule:pgc:style")
    for w in ("综艺", "真人秀", "脱口秀", "选秀", "真人秀"):
        if w in title:
            return (False, "标题含娱乐类型(%s)" % w, "rule:pgc:title")
    if ttype == PGC_DOC_TYPE:
        return (True, "纪录片类知识内容", "rule:pgc:doc")
    if ttype in PGC_ENTERTAIN_TYPES:
        return (False, "影视/综艺类娱乐内容(type=%s)" % ttype, "rule:pgc:type")
    return None


PGC_PROMPT = """你是 B 站 PGC 内容分类器（番剧/综艺/影视/纪录片/课堂）。判断这部作品是否属于"知识性内容"。

判定为知识性（knowledge=true）如果核心是传递可学习的知识，例如：
- 纪录片：自然、人文、历史、科学、军事、财经、时政等纪实与深度报道
- 科普/公开课/讲座/教学动画/编程教学动画
- 行业与商业分析、财经解读、学术讲座
- 职业技能培训课程（课堂 cheese）

判定为非知识性（knowledge=false）如果核心是娱乐消遣，例如：
- 综艺、真人秀、选秀、脱口秀、恋爱/社交观察真人秀
- 动画番剧（剧情、战斗、恋爱、搞笑），游戏/电竞/音乐/舞蹈/美食/旅行综艺
- 电视剧、电影、网剧

重要说明：
- "纪录片"属于知识性内容（除非是娱乐向真人秀类纪录片）
- 动画番剧一律按非知识处理，除非标题/简介明确是科普或教学动画
- 综艺、真人秀一律按非知识处理
- 只看核心内容，不要因为题材冷门就判 false

只输出一个 JSON 对象，不要任何解释性文字：
{"knowledge": true 或 false, "reason": "不超过15个字的依据"}

作品信息：
标题：%(title)s
简介：%(desc)s
类型标签：%(styles)s
作品类型：%(ptype)s
单集标题：%(eptitle)s
"""


def llm_pgc_verdict(meta):
    prompt = PGC_PROMPT % {
        "title": (meta.get("title") or "")[:120],
        "desc": (meta.get("desc") or "")[:400],
        "styles": "、".join(meta.get("styles") or []),
        "ptype": meta.get("type") or "",
        "eptitle": meta.get("ep_title") or "",
    }
    deadline = time.time() + LLM_DEADLINE
    for model in MODELS:
        for attempt in range(2):
            if time.time() >= deadline:
                break
            try:
                txt = oc_ask(model, prompt, deadline)
            except Exception as e:
                log("pgc llm model=%s attempt=%d failed: %s" % (model, attempt, str(e)[:120]))
                time.sleep(0.4)
                continue
            r = parse_verdict(txt)
            if r:
                return (r[0], r[1], "llm:" + model)
            log("pgc llm model=%s unparsable: %s" % (model, txt[:120]))
    return None


def _pgc_key(id_type, id_val, meta=None):
    """缓存键：优先用 media_id（同一剧集的所有集共享结论）"""
    if meta and meta.get("media_id"):
        return "md:" + str(meta["media_id"])
    return (id_type or "ep") + ":" + str(id_val)


def _pgc_obj(key, knowledge, reason, source, model, title):
    return {"key": str(key), "known": True, "knowledge": bool(knowledge), "reason": reason,
            "source": source, "model": model, "title": title}


def judge_pgc(id_type, id_val, force=False):
    meta = fetch_pgc_meta(id_type, id_val)
    key = _pgc_key(id_type, id_val, meta)
    v = get_pgc_verdict(key)
    if v and not force:
        return v
    title = meta.get("title") or ""
    if not title:
        v = _pgc_obj(key, True, "作品信息不可用，暂放行", "fallback:nometa", "", "")
        put_pgc_verdict(key, True, v["reason"], v["source"], "", "")
        return v
    rv = pgc_rule_verdict(meta)
    if rv:
        put_pgc_verdict(key, rv[0], rv[1], rv[2], "", title)
        log("pgc verdict %s knowledge=%s source=%s title=%s" % (key, rv[0], rv[2], title[:30]))
        return _pgc_obj(key, rv[0], rv[1], rv[2], "", title)
    lv = llm_pgc_verdict(meta)
    if lv:
        put_pgc_verdict(key, lv[0], lv[1], "llm", lv[2], title)
        log("pgc verdict %s knowledge=%s model=%s reason=%s title=%s"
            % (key, lv[0], lv[2], lv[1], title[:30]))
        return _pgc_obj(key, lv[0], lv[1], "llm", lv[2], title)
    v = _pgc_obj(key, True, "模型不可用，按规则兜底放行", "fallback:rule", "", title)
    put_pgc_verdict(key, True, v["reason"], v["source"], "", title)
    log("pgc verdict %s FALLBACK source=fallback:rule title=%s" % (key, title[:30]))
    return v


def judge_pgc_bounded(id_type, id_val, maxwait):
    """最多等 maxwait 秒；超时返回 None，后台线程继续判并写缓存"""
    box = {}
    done = threading.Event()

    def run():
        try:
            box["v"] = judge_pgc(id_type, id_val)
        except Exception as e:
            log("pgc thread error %s: %s" % (id_val, str(e)[:120]))
            box["v"] = None
        finally:
            done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    done.wait(max(0.1, maxwait))
    return box.get("v")


_pgc_q = queue.Queue()
_pgc_inflight = set()
_pgc_lock = threading.Lock()


def enqueue_pgc(id_type, id_val):
    k = "%s:%s" % (id_type, id_val)
    with _pgc_lock:
        if k in _pgc_inflight:
            return False
        _pgc_inflight.add(k)
    _pgc_q.put((id_type, id_val))
    return True


def _pgc_worker():
    while True:
        id_type, id_val = _pgc_q.get()
        try:
            judge_pgc(id_type, id_val)
        except Exception as e:
            log("pgc worker error %s: %s" % (id_val, str(e)[:150]))
        finally:
            with _pgc_lock:
                _pgc_inflight.discard("%s:%s" % (id_type, id_val))
            _pgc_q.task_done()


# ---------------------------------------------------------------- 直播房间
LIVE_ENTERTAIN_AREAS = {
    "娱乐", "聊天", "颜值", "才艺", "游戏", "网游", "手游", "舞蹈", "音乐", "电台",
    "情感", "美食", "美食生活", "运动", "户外", "汽车", "时尚", "虚拟主播", "美妆",
    "穿搭", "生活", "二次元", "动漫", "绘画", "摄影", "桌游", "棋牌", "台球",
    "连麦", "pk", "语音", "陪伴", "闲聊", "水聊", "户外直播", "主机游戏",
}


def get_live_verdict(room_id):
    row = db().execute(
        "SELECT knowledge, reason, source, model, title FROM live_verdicts WHERE room_id=?",
        (str(room_id),)).fetchone()
    if not row:
        return None
    return {"room_id": str(room_id), "known": True, "knowledge": bool(row[0]),
            "reason": row[1] or "", "source": row[2] or "", "model": row[3] or "",
            "title": row[4] or ""}


def delete_live_verdict(room_id):
    db().execute("DELETE FROM live_verdicts WHERE room_id=?", (str(room_id),))
    db().commit()


def put_live_verdict(room_id, knowledge, reason, source, model="", title=""):
    db().execute(
        "INSERT OR REPLACE INTO live_verdicts(room_id, knowledge, reason, source, model, title, ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (str(room_id), 1 if knowledge else 0, reason, source, model, title, int(time.time())))
    db().commit()


def fetch_live_meta(room_id):
    """抓直播间信息（公开 API，无需 cookie）"""
    out = {"room_id": str(room_id), "title": "", "desc": "", "area": "", "area_detail": "",
           "tags": [], "uid": 0, "live_status": 0}
    try:
        raw = http_get("https://api.live.bilibili.com/room/v1/Room/get_info?room_id=" + str(room_id),
                       referer="https://live.bilibili.com/")
        j = json.loads(raw.decode("utf-8", "replace"))
        d = j.get("data") or {}
        out["title"] = d.get("title") or d.get("room_name") or ""
        out["desc"] = (d.get("description") or "").strip()
        out["area"] = d.get("parent_area_name") or ""
        out["area_detail"] = d.get("area_name") or ""
        out["tags"] = [t for t in (d.get("tags") or "").split(",") if t.strip()][:12]
        out["uid"] = d.get("uid") or 0
        out["live_status"] = d.get("live_status") or 0
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def live_rule_verdict(meta):
    """直播规则层：娱乐类分区直接判非知识；标题/标签命中娱乐词也判非知识；否则交给 LLM"""
    area = (meta.get("area") or "").strip()
    if area and area.lower() in LIVE_ENTERTAIN_AREAS:
        return (False, "直播分区为娱乐类(%s)" % area, "rule:live:area")
    text = " ".join([meta.get("title") or "", meta.get("desc") or "", meta.get("area_detail") or "",
                     " ".join(meta.get("tags") or [])])
    r = classify_text(text)
    if r is None:
        return None
    return r


LIVE_PROMPT = """你是 B 站直播内容分类器。判断下面这个直播间当前播出的内容是否属于"知识性内容"。

判定为知识性（knowledge=true）如果直播核心是传递可学习的知识，例如：
- 学科/考试/复习/解题讲解
- 编程、IT、AI、算法、数据分析等技术教学与原理讲解
- 科普、学术讲座、公开课、论文解读、实验与数据
- 职业技能培训（乐器、绘画、摄影、剪辑、维修等教学）
- 时政新闻解读、行业与商业分析
- 语言学习、考证辅导

判定为非知识性（knowledge=false）如果直播核心是娱乐消遣或陪伴闲聊，例如：
- 游戏直播/实况、聊天互动、水聊、日常闲聊、陪伴
- 才艺表演：唱歌、跳舞、演奏、翻唱、虚拟主播
- 颜值/美妆/穿搭/日常分享/吃播/探店
- 带货广告、纯闲聊无实质内容

判定要点：
- 直播间是"实时互动"的，除非内容明确在讲解知识，否则默认 knowledge=false
- 分区只是参考，不要仅凭分区下结论；但"娱乐/聊天/颜值/才艺/游戏"类分区里的内容
  若无明确知识讲解，判 false
- 标题里有"教学/讲解/课/科普/教程/公开课/备考"等明确知识信号时，才考虑 true

只输出一个 JSON 对象，不要任何解释性文字：
{"knowledge": true 或 false, "reason": "不超过15个字的依据"}

直播间信息：
标题：%(title)s
简介：%(desc)s
分区：%(area)s / %(area_detail)s
标签：%(tags)s
"""


def llm_live_verdict(meta):
    prompt = LIVE_PROMPT % {
        "title": (meta.get("title") or "")[:120],
        "desc": (meta.get("desc") or "")[:300],
        "area": meta.get("area") or "",
        "area_detail": meta.get("area_detail") or "",
        "tags": "、".join(meta.get("tags") or []),
    }
    deadline = time.time() + LLM_DEADLINE
    for model in MODELS:
        for attempt in range(2):
            if time.time() >= deadline:
                break
            try:
                txt = oc_ask(model, prompt, deadline)
            except Exception as e:
                log("live llm model=%s attempt=%d failed: %s" % (model, attempt, str(e)[:120]))
                time.sleep(0.4)
                continue
            r = parse_verdict(txt)
            if r:
                return (r[0], r[1], "llm:" + model)
            log("live llm model=%s unparsable: %s" % (model, txt[:120]))
    return None


def _live_verdict_obj(room_id, knowledge, reason, source, model, title):
    return {"room_id": str(room_id), "known": True, "knowledge": bool(knowledge),
            "reason": reason, "source": source, "model": model, "title": title}


def judge_live(room_id, force=False):
    room_id = str(room_id)
    v = get_live_verdict(room_id)
    if v and not force:
        return v
    meta = fetch_live_meta(room_id)
    title = meta.get("title") or ""
    if not title:
        v = _live_verdict_obj(room_id, True, "直播间信息不可用，暂放行", "fallback:nometa", "", "")
        put_live_verdict(room_id, True, v["reason"], v["source"], "", "")
        return v
    rv = live_rule_verdict(meta)
    if rv:
        put_live_verdict(room_id, rv[0], rv[1], rv[2], "", title)
        log("live verdict %s knowledge=%s source=%s title=%s" % (room_id, rv[0], rv[2], title[:30]))
        return _live_verdict_obj(room_id, rv[0], rv[1], rv[2], "", title)
    lv = llm_live_verdict(meta)
    if lv:
        put_live_verdict(room_id, lv[0], lv[1], "llm", lv[2], title)
        log("live verdict %s knowledge=%s model=%s reason=%s title=%s"
            % (room_id, lv[0], lv[2], lv[1], title[:30]))
        return _live_verdict_obj(room_id, lv[0], lv[1], "llm", lv[2], title)
    v = _live_verdict_obj(room_id, True, "模型不可用，按规则兜底放行", "fallback:rule", "", title)
    put_live_verdict(room_id, True, v["reason"], v["source"], "", title)
    log("live verdict %s FALLBACK source=fallback:rule title=%s" % (room_id, title[:30]))
    return v


def judge_live_bounded(room_id, maxwait):
    """最多等 maxwait 秒；超时返回 None，后台线程继续判并写缓存"""
    box = {}
    done = threading.Event()

    def run():
        try:
            box["v"] = judge_live(room_id)
        except Exception as e:
            log("live thread error %s: %s" % (room_id, str(e)[:120]))
            box["v"] = None
        finally:
            done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    done.wait(max(0.1, maxwait))
    return box.get("v")


_live_q = queue.Queue()
_live_inflight = set()
_live_lock = threading.Lock()


def enqueue_live(room_id):
    room_id = str(room_id)
    with _live_lock:
        if room_id in _live_inflight:
            return False
        _live_inflight.add(room_id)
    _live_q.put(room_id)
    return True


def _live_worker():
    while True:
        room_id = _live_q.get()
        try:
            if get_live_verdict(room_id):
                continue
            judge_live(room_id)
        except Exception as e:
            log("live worker error %s: %s" % (room_id, str(e)[:150]))
        finally:
            with _live_lock:
                _live_inflight.discard(room_id)
            _live_q.task_done()


# ---------------------------------------------------------------- 元数据
def http_get(url, timeout=META_TIMEOUT, referer="https://www.bilibili.com/", ua=None, headers=None):
    h = {"User-Agent": (ua or UA), "Referer": referer}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch_meta(bvid):
    """抓取视频元数据（服务端直取，无需 cookie，实测 ~170ms）"""
    out = {"bvid": bvid, "aid": 0, "title": "", "desc": "", "tags": [], "tname": "",
           "duration": 0, "owner": ""}
    try:
        raw = http_get("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
        j = json.loads(raw.decode("utf-8", "replace"))
        d = j.get("data") or {}
        out["title"] = d.get("title") or ""
        out["desc"] = (d.get("desc") or "").strip()
        out["tname"] = d.get("tname") or ""
        out["duration"] = d.get("duration") or 0
        out["aid"] = d.get("aid") or 0
        out["owner"] = ((d.get("owner") or {}).get("name")) or ""
        out["tid"] = d.get("tid")
    except Exception as e:
        out["error"] = str(e)[:80]
    if out.get("title"):
        try:
            raw = http_get("https://api.bilibili.com/x/tag/archive/tags?bvid=" + bvid)
            j = json.loads(raw.decode("utf-8", "replace"))
            out["tags"] = [t.get("tag_name") for t in (j.get("data") or []) if t.get("tag_name")][:12]
        except Exception:
            pass
    return out


def meta_text(meta):
    parts = [meta.get("title") or "", meta.get("desc") or "", meta.get("tname") or ""]
    parts.extend(meta.get("tags") or [])
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- 规则层
def rule_verdict(meta):
    """分层规则裁决：具体游戏名 > 强知识白名单 > 娱乐黑名单 > 泛游戏词；
    都不命中 -> None（交 LLM）。详见 rules.classify_text。"""
    text = meta_text(meta)
    r = classify_text(text)
    if r is None:
        return None
    knowledge, reason, source = r
    return (knowledge, reason, source)


# ---------------------------------------------------------------- LLM 层
PROMPT = """你是 B 站视频内容分类器。判断下面的视频是否属于"知识性内容"。

判定为知识性（knowledge=true）如果视频核心是传递可学习的知识，例如：
- 学科/考试/复习/解题/试卷讲解
- 编程、IT、AI、算法、数据分析等技术教程与原理讲解
- 科普、学术研究、论文解读、实验与数据
- 职业技能培训（乐器、绘画、摄影、剪辑、维修等教学）
- 纪录片、公开课、讲座、时政新闻解读、行业与商业分析
- 语言学习、考证辅导

判定为非知识性（knowledge=false）如果视频核心是娱乐消遣，例如：
- 游戏实况/攻略/直播、纯搞笑段子、日常 vlog、生活记录
- 明星八卦、网红整活、鬼畜、舞蹈、翻唱、聊天闲聊
- 带货广告、纯剪辑混剪、无实质内容的剪辑
- 情感吐槽、搞笑合集

以下情况一律判 knowledge=false（重点误判防范）：
- 标题/标签含"搞笑/鬼畜/整活/剧情/实况/反应/开箱/吃播/日常/vlog/二次元/娘化/轻小说"等娱乐向词
- AI 生成或 AI 二创的短片、动画、配音作品：即使标签含 ai、prompt、claude、gpt、
  seedance、gemini 等，只要不是在讲技术原理/模型用法，就是 false
- 宠物日常、萌宠内容
- 影视/动漫/游戏剧情解说、MV、翻唱、演奏、鬼畜 remix
- 游戏攻略、游戏教学、游戏实况（游戏内容一律按娱乐处理）
- 只有"如何…"的搞笑情节演绎（例如"如何反击某梗""如何整蛊"），不是系统性知识讲解
- 单纯展示/吐槽/测评产品、开箱，缺少可复用的方法与原理

判定为知识性时，视频必须能让人学到可复用的知识（原理、方法、代码、公式、步骤、结论）。
如果看完只是"哈哈一笑"或"看过个乐子"，就判 false。

只输出一个 JSON 对象，不要任何解释性文字：
{"knowledge": true 或 false, "reason": "不超过15个字的依据"}

视频信息：
标题：%(title)s
简介：%(desc)s
分区：%(tname)s
时长：%(duration)s 秒
UP主：%(owner)s
标签：%(tags)s
弱知识线索（仅参考，可能与娱乐内容重叠，不要据此直接下结论）：%(weak)s
"""

_sess_lock = threading.Lock()
_sessions = {}   # model -> {"sid":..., "uses":...}


def oc_new_session(model):
    body = json.dumps({"title": "kb-judge"}).encode()
    req = urllib.request.Request(OC_BASE + "/session", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    j = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    return j.get("id")


def oc_ask(model, prompt, deadline):
    """调用常驻 opencode serve 的 session message 接口
    注意：opencode 免费层对"自定义 agent"会直接返回 FreeTierError(403)，
    因此这里用默认 agent + per-call 禁用全部工具（分类器不允许执行任何命令）。"""
    with _sess_lock:
        st = _sessions.get(model)
        if not st or st["uses"] >= 3:
            st = {"sid": oc_new_session(model), "uses": 0}
            _sessions[model] = st
        sid = st["sid"]
    payload = {
        "model": {"providerID": model.split("/")[0], "modelID": model.split("/")[-1]},
        "parts": [{"type": "text", "text": prompt}],
        # 注意：不能传 "tools" 字段——opencode 免费层会直接返回
        # FreeTierError(403) "free tier can only be used from within OpenCode"。
        # 改为用 system 强约束"不调用任何工具"，并在解析时拒绝任何 tool 调用。
        "system": ("你是纯分类器。不要调用任何工具、不要读写文件、不要执行命令，"
                   "只输出一个 JSON 对象。"),
    }
    # 严格遵守 deadline：原来 max(3.0, ...) 会让一次调用越过截止时间 3 秒
    timeout = max(0.5, min(LLM_TIMEOUT, deadline - time.time()))
    body = json.dumps(payload).encode()
    req = urllib.request.Request(OC_BASE + "/session/" + sid + "/message", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    j = json.loads(raw)
    if (j.get("info") or {}).get("error"):
        raise RuntimeError(((j["info"].get("error") or {}).get("data") or {}).get("message", "api error")[:120])
    parts = j.get("parts") or []
    if any(p.get("type") == "tool" for p in parts):
        raise RuntimeError("model attempted a tool call; rejected")
    texts = [p.get("text") for p in parts if p.get("type") == "text"]
    with _sess_lock:
        st = _sessions.get(model)
        if st:
            st["uses"] += 1
    return "\n".join([t for t in texts if t])


VERDICT_RE = re.compile(r"\{[^{}]*\"knowledge\"[^{}]*\}", re.IGNORECASE)


def parse_verdict(text):
    if not text:
        return None
    m = VERDICT_RE.search(text)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except Exception:
        return None
    if "knowledge" not in j:
        return None
    v = j.get("knowledge")
    if isinstance(v, str):
        v = v.strip().lower() in ("true", "yes", "1")
    if not isinstance(v, bool):
        return None
    return (v, str(j.get("reason", ""))[:40])


def weak_hits(meta, limit=6):
    """命中的弱知识词，仅作为 LLM 参考线索（不作判定依据）"""
    low = meta_text(meta).lower()
    out = []
    for w in KNOWLEDGE_WEAK:
        if w.lower() in low:
            out.append(w)
            if len(out) >= limit:
                break
    return out


def llm_verdict(meta):
    prompt = PROMPT % {
        "title": meta.get("title", ""),
        "desc": (meta.get("desc", "") or "")[:300],
        "tname": meta.get("tname", ""),
        "duration": meta.get("duration", 0),
        "owner": meta.get("owner", ""),
        "tags": "、".join(meta.get("tags") or []),
        "weak": "、".join(weak_hits(meta)) or "无",
    }
    deadline = time.time() + LLM_DEADLINE
    for model in MODELS:
        for attempt in range(2):
            if time.time() >= deadline:
                break
            try:
                txt = oc_ask(model, prompt, deadline)
            except Exception as e:
                log("llm model=%s attempt=%d failed: %s" % (model, attempt, str(e)[:120]))
                time.sleep(0.4)
                continue
            r = parse_verdict(txt)
            if r:
                return (r[0], r[1], "llm:" + model)
            log("llm model=%s unparsable: %s" % (model, txt[:120]))
    return None


# ---------------------------------------------------------------- 裁决
_meta_cache = {}     # bvid -> (ts, meta)
_meta_lock = threading.Lock()


def get_meta(bvid):
    with _meta_lock:
        hit = _meta_cache.get(bvid)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    meta = fetch_meta(bvid)
    with _meta_lock:
        if len(_meta_cache) > 2000:
            _meta_cache.clear()
        _meta_cache[bvid] = (time.time(), meta)
    return meta


def _manual_guard(bvid):
    """人工裁决(source=manual)优先级最高，自动流程直接跳过"""
    v = get_verdict(bvid)
    if v and v.get("source") == "manual":
        return v
    return None


def quick_verdict(bvid):
    """规则层快速裁决（~200ms）；命中才返回，None 表示需要 LLM"""
    m = _manual_guard(bvid)
    if m:
        return m
    meta = get_meta(bvid)
    title = meta.get("title", "")
    if not title:
        v = {"bvid": bvid, "known": True, "knowledge": True,
             "reason": "元数据不可用，暂放行", "source": "fallback:nometa", "model": "",
             "title": "", "ts": int(time.time())}
        put_verdict(v)
        return v
    rv = rule_verdict(meta)
    if not rv:
        return None
    knowledge, reason, source = rv
    v = {"bvid": bvid, "known": True, "knowledge": knowledge, "reason": reason,
         "source": source, "model": "", "title": title, "ts": int(time.time()),
         "aid": meta.get("aid") or 0, "cat": classify_category(meta_text(meta))}
    put_verdict(v)
    clear_pending(bvid)
    log("verdict %s knowledge=%s source=%s title=%s" % (bvid, knowledge, source, title[:30]))
    return v


def llm_only_verdict(bvid):
    """需要 LLM 的部分；模型全挂 -> 规则兜底放行并记入待复判"""
    m = _manual_guard(bvid)
    if m:
        return m
    meta = get_meta(bvid)
    title = meta.get("title", "")
    if not title:
        # 元数据取不到（无效/不存在的 BV 号）：不交给 LLM，直接放行
        v = {"bvid": bvid, "known": True, "knowledge": True,
             "reason": "元数据不可用，暂放行", "source": "fallback:nometa", "model": "",
             "title": "", "ts": int(time.time())}
        put_verdict(v)
        return v
    lv = llm_verdict(meta)
    if lv:
        knowledge, reason, model = lv
        v = {"bvid": bvid, "known": True, "knowledge": knowledge, "reason": reason,
             "source": "llm", "model": model, "title": title, "ts": int(time.time()),
             "aid": meta.get("aid") or 0, "cat": classify_category(meta_text(meta))}
        put_verdict(v)
        clear_pending(bvid)
        log("verdict %s knowledge=%s model=%s reason=%s title=%s"
            % (bvid, knowledge, model, reason, title[:30]))
        return v
    v = {"bvid": bvid, "known": True, "knowledge": True,
         "reason": "模型不可用，按规则兜底放行", "source": "fallback:rule", "model": "",
         "title": title, "ts": int(time.time()), "aid": meta.get("aid") or 0}
    put_verdict(v)
    mark_pending(bvid, title, "llm-unavailable")
    log("verdict %s FALLBACK source=fallback:rule title=%s" % (bvid, title[:30]))
    return v


def judge_one(bvid, force=False):
    v = get_verdict(bvid)
    if v and not force:
        return v
    q = quick_verdict(bvid)
    if q:
        return q
    return llm_only_verdict(bvid)


# ---------------------------------------------------------------- 队列（两级流水线）
# 快队列：先跑规则层（~200ms）；模糊项才进 LLM 慢队列，避免规则可判的视频被 LLM 堵住
_q_prio = queue.Queue()      # 用户点击（优先）
_q_bg = queue.Queue()        # 后台预判
_q_llm_prio = queue.Queue()
_q_llm_bg = queue.Queue()
_inflight = set()
_infl_lock = threading.Lock()


def enqueue(bvid, force=False, prio=True):
    with _infl_lock:
        if bvid in _inflight:
            return False
        # 后台预判限流：避免把免费模型额度打爆（点击判定永远优先）
        if not prio and _q_bg.qsize() > 30:
            return False
        _inflight.add(bvid)
    (_q_prio if prio else _q_bg).put((bvid, force))
    return True


def _unlock(bvid):
    with _infl_lock:
        _inflight.discard(bvid)


def _fast_worker(q, llm_q):
    while True:
        bvid, force = q.get()
        try:
            if not force and get_verdict(bvid):
                continue
            if quick_verdict(bvid) is None:
                llm_q.put((bvid, force))       # 规则判不了 -> 交给 LLM
            else:
                _unlock(bvid)
        except Exception as e:
            log("fast worker error %s: %s" % (bvid, str(e)[:200]))
            _unlock(bvid)
        finally:
            q.task_done()


def _llm_worker(q):
    while True:
        bvid, force = q.get()
        try:
            llm_only_verdict(bvid)
        except Exception as e:
            log("llm worker error %s: %s" % (bvid, str(e)[:200]))
        finally:
            _unlock(bvid)
            q.task_done()


def recheck_loop():
    """模型恢复后复判 pending（待复判项直接走 LLM，跳过规则层）"""
    while True:
        time.sleep(240)
        try:
            for item in pending_list(10):
                bvid = item["bvid"]
                if item.get("reason") == "metadata-unavailable":
                    clear_pending(bvid)
                    continue
                with _infl_lock:
                    if bvid in _inflight:
                        continue
                    _inflight.add(bvid)
                _q_llm_bg.put((bvid, True))
                time.sleep(2)
        except Exception as e:
            log("recheck error: %s" % str(e)[:200])


# ---------------------------------------------------------------- 日志
_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        sys.stderr.write("[kb-judge] %s %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "ts": int(time.time())})
            if path == "/verdict":
                bvid = (q.get("bvid") or [""])[0]
                aid = (q.get("aid") or [""])[0]
                if not bvid and not aid:
                    return self._send(400, {"error": "bvid or aid required"})
                v = get_verdict(bvid) if bvid else get_verdict_by_aid(aid)
                if v:
                    return self._send(200, v)
                return self._send(200, {"bvid": bvid, "known": False, "knowledge": False,
                                        "reason": "", "source": ""})
            if path == "/verdicts":
                bvids = q.get("bvid") or []
                res = {}
                for b in bvids[:60]:
                    v = get_verdict(b)
                    if v:
                        res[b] = v
                return self._send(200, {"results": res})
            if path == "/judge":
                bvid = (q.get("bvid") or [""])[0]
                if not bvid:
                    return self._send(400, {"error": "bvid required"})
                force = (q.get("force") or ["0"])[0] == "1"
                v = get_verdict(bvid)
                if v and not force:
                    return self._send(200, v)
                prio = (q.get("prio") or ["1"])[0] != "0"
                enqueue(bvid, force=force, prio=prio)
                return self._send(200, {"bvid": bvid, "known": False, "queued": True,
                                        "forced": force})
            if path == "/web-verdict":
                host = (q.get("host") or [""])[0].strip().lower()
                if not host:
                    return self._send(400, {"error": "host required"})
                title = (q.get("title") or [""])[0]
                desc = (q.get("desc") or [""])[0]
                wait = (q.get("wait") or ["0"])[0] == "1"
                rv = web_rule_verdict(host, title, desc)
                if rv:
                    put_web_verdict(host, rv[0], rv[1], rv[2], "", title)
                    return self._send(200, {"host": host, "known": True, "knowledge": rv[0],
                                            "reason": rv[1], "source": rv[2]})
                v = get_web_verdict(host)
                if v and str(v.get("source") or "").startswith("rule:") and not rv:
                    delete_web_verdict(host)      # 规则层不再命中 -> 缓存作废，重新判定
                    v = None
                if v:
                    return self._send(200, v)
                if wait:
                    try:
                        mw = float((q.get("maxwait") or ["8"])[0])
                    except Exception:
                        mw = 8.0
                    mw = max(0.5, min(mw, LLM_DEADLINE))
                    lv = llm_web_verdict_bounded(host, title, desc, mw)
                    if lv:
                        put_web_verdict(host, lv[0], lv[1], "llm", lv[2], title)
                        return self._send(200, {"host": host, "known": True, "knowledge": lv[0],
                                                "reason": lv[1], "source": "llm", "model": lv[2]})
                    enqueue_web(host, title, desc)   # 超时 -> 后台继续判，下次访问即命中缓存
                    return self._send(200, {"host": host, "known": False, "queued": True,
                                            "reason": "判定超时，先放行"})
                enqueue_web(host, title, desc)
                return self._send(200, {"host": host, "known": False, "queued": True})
            if path == "/subject-feed":
                try:
                    lim = int((q.get("limit") or ["24"])[0])
                except Exception:
                    lim = 24
                items = subject_feed(max(1, min(lim, 60)))
                return self._send(200, {"items": items, "ts": int(time.time())})
            if path == "/pgc-verdict":
                id_type = (q.get("type") or ["ep"])[0].strip().lower()
                id_val = (q.get("id") or [""])[0].strip()
                if not id_val.isdigit():
                    return self._send(400, {"error": "id required"})
                if id_type in ("ss", "season"):
                    id_type = "ss"
                elif id_type in ("md", "media"):
                    id_type = "md"
                else:
                    id_type = "ep"
                alias = "%s:%s" % (id_type, id_val)
                key = _pgc_alias.get(alias)
                if not key:
                    key = ("md:" + id_val) if id_type == "md" else alias
                v = get_pgc_verdict(key)
                if v is None and id_type in ("ep", "ss"):
                    # ep/ss -> media_id 映射后重查（同一剧集所有集共享结论）
                    meta = fetch_pgc_meta(id_type, id_val)
                    mkey = "md:" + str(meta.get("media_id") or "")
                    if mkey != "md:":
                        v = get_pgc_verdict(mkey)
                        if v:
                            key = mkey
                if v and str(v.get("source") or "").startswith("rule:pgc:"):
                    if not pgc_rule_verdict(fetch_pgc_meta(id_type, id_val)):
                        db().execute("DELETE FROM pgc_verdicts WHERE key=?", (v.get("key") or key,))
                        db().commit()
                        v = None
                if v:
                    return self._send(200, v)
                if (q.get("wait") or ["0"])[0] == "1":
                    try:
                        mw = float((q.get("maxwait") or ["8"])[0])
                    except Exception:
                        mw = 8.0
                    mw = max(0.5, min(mw, LLM_DEADLINE))
                    lv = judge_pgc_bounded(id_type, id_val, mw)
                    if lv:
                        return self._send(200, lv)
                    enqueue_pgc(id_type, id_val)
                    return self._send(200, {"key": "%s:%s" % (id_type, id_val),
                                            "known": False, "queued": True, "reason": "判定超时"})
                enqueue_pgc(id_type, id_val)
                return self._send(200, {"key": "%s:%s" % (id_type, id_val),
                                        "known": False, "queued": True})
            if path == "/live-verdict":
                room_id = (q.get("room_id") or [""])[0].strip()
                if not room_id.isdigit():
                    return self._send(400, {"error": "room_id required"})
                v = get_live_verdict(room_id)
                if v and str(v.get("source") or "").startswith("rule:live:"):
                    # 规则层不再命中 -> 缓存作废，重新判定
                    if not live_rule_verdict(fetch_live_meta(room_id)):
                        delete_live_verdict(room_id)
                        v = None
                if v:
                    return self._send(200, v)
                if (q.get("wait") or ["0"])[0] == "1":
                    try:
                        mw = float((q.get("maxwait") or ["8"])[0])
                    except Exception:
                        mw = 8.0
                    mw = max(0.5, min(mw, LLM_DEADLINE))
                    lv = judge_live_bounded(room_id, mw)
                    if lv:
                        return self._send(200, lv)
                    enqueue_live(room_id)
                    return self._send(200, {"room_id": room_id, "known": False,
                                            "queued": True, "reason": "判定超时"})
                enqueue_live(room_id)
                return self._send(200, {"room_id": room_id, "known": False, "queued": True})
            if path == "/web-stats":
                with _db_lock:
                    total = db().execute("SELECT COUNT(*) FROM web_verdicts").fetchone()[0]
                    k = db().execute("SELECT COUNT(*) FROM web_verdicts WHERE knowledge=1").fetchone()[0]
                    bysrc = db().execute(
                        "SELECT source,COUNT(*) FROM web_verdicts GROUP BY source").fetchall()
                    top = db().execute(
                        "SELECT host,COUNT(*) c FROM web_verdicts GROUP BY host ORDER BY c DESC LIMIT 10"
                    ).fetchall()
                return self._send(200, {"total": total, "knowledge": k,
                                        "non_knowledge": total - k, "by_source": dict(bysrc),
                                        "top": [{"host": t[0], "hits": t[1]} for t in top],
                                        "queue": _web_q.qsize()})
            if path == "/stats":
                with _db_lock:
                    total = db().execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
                    k = db().execute("SELECT COUNT(*) FROM verdicts WHERE knowledge=1").fetchone()[0]
                    pend = db().execute("SELECT COUNT(*) FROM pending").fetchone()[0]
                    bysrc = db().execute(
                        "SELECT source,COUNT(*) FROM verdicts GROUP BY source").fetchall()
                return self._send(200, {"total": total, "knowledge": k, "non_knowledge": total - k,
                                        "pending": pend, "by_source": dict(bysrc),
                                        "queue_prio": _q_prio.qsize(), "queue_bg": _q_bg.qsize()})
            if path == "/audit":
                lim = int((q.get("limit") or ["50"])[0])
                with _db_lock:
                    rows = db().execute(
                        "SELECT bvid,knowledge,reason,source,model,title,ts FROM verdicts"
                        " ORDER BY ts DESC LIMIT ?", (lim,)).fetchall()
                return self._send(200, {"rows": [
                    {"bvid": r[0], "knowledge": bool(r[1]), "reason": r[2], "source": r[3],
                     "model": r[4], "title": r[5], "ts": r[6]} for r in rows]})
        except Exception as e:
            return self._send(500, {"error": str(e)[:200]})
        self._send(404, {"error": "not found"})


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db()
    threading.Thread(target=_fast_worker, args=(_q_prio, _q_llm_prio), daemon=True).start()
    threading.Thread(target=_fast_worker, args=(_q_bg, _q_llm_bg), daemon=True).start()
    for _ in range(2):
        threading.Thread(target=_fast_worker, args=(_q_bg, _q_llm_bg), daemon=True).start()
    threading.Thread(target=_llm_worker, args=(_q_llm_prio,), daemon=True).start()
    threading.Thread(target=_llm_worker, args=(_q_llm_bg,), daemon=True).start()
    threading.Thread(target=recheck_loop, daemon=True).start()
    threading.Thread(target=_web_worker, daemon=True).start()
    threading.Thread(target=_live_worker, daemon=True).start()
    threading.Thread(target=_pgc_worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log("judge service on %s:%d db=%s models=%s" % (HOST, PORT, DB_PATH, ",".join(MODELS)))
    srv.serve_forever()


if __name__ == "__main__":
    main()
