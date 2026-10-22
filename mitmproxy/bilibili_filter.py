"""
bilibili_filter.py - mitmproxy addon（v3：知识性内容门禁）

四层能力：
1) URL 层：搜索关键词/游戏区路径 -> 403 屏蔽页
2) API 层：JSON 响应剔除"游戏"分区条目 + 标题命中游戏词的条目；记录游戏 bvid
3) 页面层：/video/BVx 打开时向 judge 服务查询"知识性裁决"
           - 知识性 -> 放行
           - 非知识 -> 403 拦截页（带理由）
           - 未裁决 -> 返回"核验中"落地页，前端轮询裁决结果后决定放行/拦截
4) 播放流层：/x/player/playurl 对已判定非知识的视频直接断流（防绕过）
   首页/搜索流里的视频卡片会异步上报 bvid 预判（后台 LLM 裁决），点击时零延迟
词表在 $KB_RULES_DIR/rules.py（与裁决服务共用），改完 systemctl restart mitmfilter
"""
import json
import os
import queue
import re
import sys
import threading
import urllib.parse

from mitmproxy import ctx
from mitmproxy import http

RULES_DIR = os.environ.get("KB_RULES_DIR", "/opt/kbjudge")
sys.path.insert(0, RULES_DIR)
from rules import GAME_ALL, MATH_WORDS, PHYSICS_WORDS  # noqa: E402  单一事实源（泛游戏词+具体游戏名）
from rules import (SUBJECT_WORDS, TECH_WORDS, POP_SCI_WORDS,  # noqa: E402  学科类目词表
                   SKILL_WORDS)

# ---------------- 关键词（游戏/娱乐黑名单）----------------
KEYWORDS = sorted(set(GAME_ALL))
KW_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)

GAME_PARTITION = "游戏"          # 视频分区名
GAME_RANK_PATH = "rank/game"     # 游戏区排行榜 URL 片段

# 只对 bilibili 系域名注入
INJECT_HOSTS = (".bilibili.com", ".bilivideo.com", ".b23.tv", ".bilibili.tv")
# 拦截关键词的请求域名（搜索页、搜索 API）
BLOCK_URL_HOSTS = ("search.bilibili.com", "api.bilibili.com", "s.search.bilibili.com")
INJECT_MARKER = "__bili_kw_filter__"

# 记录"确认为游戏视频"的 bvid，用于拦截播放流
GAME_BVIDS = set()

# ---------------- 知识性裁决服务 ----------------
JUDGE_URL = os.environ.get("KB_JUDGE_URL", "http://127.0.0.1:8090")
JUDGE_TIMEOUT = float(os.environ.get("KB_JUDGE_TIMEOUT", "2.5"))
# 浏览器与代理之间的内部接口（由本插件本地应答，不走上游）
KB_API_PREFIX = ("/kb-verify", "/kb-start", "/kb-batch", "/kb-web-verify",
                 "/kb-live-verify", "/kb-pgc-verify", "/kb-subject-feed")
# 直播间：live.bilibili.com/<room_id>
LIVE_ROOM_RE = re.compile(r"^/(\d{1,20})/?$")
# 番剧/综艺/影视/纪录片：/bangumi/play/ep123、/bangumi/play/ss456、/bangumi/media/md789
PGC_EP_RE = re.compile(r"^/bangumi/play/ep(\d+)")
PGC_SS_RE = re.compile(r"^/bangumi/play/ss(\d+)")
PGC_MD_RE = re.compile(r"^/bangumi/media/md(\d+)")
PGC_PLAYURL_RE = re.compile(r"^/pgc/player/web/playurl")
# PAC 端点：内网 Windows 机器把"自动配置脚本"指向 http://<本机IP>:8080/proxy.pac 即可
PAC_PATH = "/proxy.pac"
def _auto_ip():
    """自动探测本机对外 IP（PAC 需要一个客户端能访问的地址）"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


PROXY_HOST = os.environ.get("KB_PROXY_HOST") or _auto_ip()
PROXY_PORT = os.environ.get("KB_PROXY_PORT", "8080")
LAN_NET = os.environ.get("KB_LAN_NET", "")
LAN_MASK = os.environ.get("KB_LAN_MASK", "255.255.255.0")
PAC_TEMPLATE = """// Bilibili knowledge-content gateway (auto proxy config)
// host: %(host)s:%(port)s  ->  mitmproxy + kb-judge on 174
function FindProxyForURL(url, host) {
  if (isPlainHostName(host) ||
      shExpMatch(host, "localhost") ||
      isInNet(host, "%(net)s", "%(mask)s")) {
    return "DIRECT";
  }
  return "PROXY %(host)s:%(port)s";
}
"""
# ---------------- 网页级知识门禁 ----------------
# KB_WEB_GATE: off=关闭 | confident=名单直接拦、未知站点等 LLM 判定(有限等待) | strict=判定失败也不放行
WEB_GATE_MODE = os.environ.get("KB_WEB_GATE", "confident").strip().lower()
WEB_WAIT = float(os.environ.get("KB_WEB_WAIT", "4"))          # 未知站点最多等待裁决的秒数
WEB_INJECT = os.environ.get("KB_WEB_INJECT", "1") not in ("0", "false", "no")
WEB_DEBUG = os.environ.get("KB_WEB_DEBUG", "0") not in ("0", "false", "no")
VIDEO_PATH_RE = re.compile(r"^/video/(BV[0-9A-Za-z]{10})")
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
BVID_BYTES_RE = re.compile(rb"BV[0-9A-Za-z]{10}")

_DBG = [0]

# 预判队列（把首页/搜索流里的 bvid 丢给 judge 后台裁决）
_pf_q = queue.Queue()
_pf_seen = set()
_pf_lock = threading.Lock()


def _pf_worker():
    ctx.log.info("[kb-gate] prefetch worker started")
    while True:
        bvids = _pf_q.get()
        if bvids is None:
            break
        for bvid in bvids:
            try:
                _judge_req("/judge?bvid=" + bvid + "&prio=0")
            except Exception:
                pass
        _pf_q.task_done()


def _prefetch(bvids):
    if not bvids:
        return
    with _pf_lock:
        new = [b for b in bvids if b not in _pf_seen]
        _pf_seen.update(new)
    if len(_pf_seen) > 4000:
        with _pf_lock:
            _pf_seen.clear()
    if new:
        ctx.log.info("[kb-gate] prefetch %d bvids: %s" % (len(new), ",".join(new[:6])))
        _pf_q.put(new)


def _client_ip(flow) -> str:
    try:
        peer = flow.client_conn.peername
        return peer[0] if peer else "?"
    except Exception:
        return "?"


def _judge_req(path, timeout=None):
    """调用本地裁决服务（只读查询，毫秒级）"""
    import urllib.request
    req = urllib.request.Request(JUDGE_URL + path)
    with urllib.request.urlopen(req, timeout=timeout or JUDGE_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------- 网页级门禁 ----------------
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(rb"<meta\b[^>]*>", re.I)
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_LAN_HOSTS = (".local", ".lan", ".internal", ".home.arpa")


def _is_local_host(host: str) -> bool:
    """内网/本机主机一律不判定（工作相关）"""
    h = (host or "").lower()
    if not h or h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if _IP_RE.match(h):
        try:
            p = [int(x) for x in h.split(".")]
        except Exception:
            return True
        if p[0] == 10 or p[0] == 127 or p[0] == 0:
            return True
        if p[0] == 172 and 16 <= p[1] <= 31:
            return True
        if p[0] == 192 and p[1] == 168:
            return True
        if p[0] == 169 and p[1] == 254:
            return True
        return False
    if any(h.endswith(x) for x in _LAN_HOSTS):
        return True
    return False


def _clean_text(b) -> str:
    try:
        s2 = b.decode("utf-8", "ignore")
    except Exception:
        s2 = str(b)
    s2 = re.sub(r"<[^>]+>", " ", s2)
    s2 = (s2.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
          .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s2).strip()[:200]


def _extract_title_desc(raw: bytes):
    head = raw[:200000]
    title = ""
    m = _TITLE_RE.search(head)
    if m:
        title = _clean_text(m.group(1))
    desc = ""
    for mm in _META_RE.finditer(head):
        tag = mm.group(0)
        if re.search(rb"name\s*=\s*[\"\']?description", tag, re.I):
            c = re.search(rb"content\s*=\s*[\"\'](.*?)[\"\']", tag, re.I | re.S)
            if c:
                desc = _clean_text(c.group(1))
                break
    return title, desc


def _web_verdict(host, title, desc):
    """问裁决服务这个站点是不是知识类；返回 dict"""
    qs = urllib.parse.urlencode({"host": host, "title": title[:120], "desc": desc[:300],
                                 "wait": "1", "maxwait": str(WEB_WAIT)})
    try:
        return _judge_req("/web-verdict?" + qs, timeout=WEB_WAIT + 3)
    except Exception as e:
        ctx.log.warn("[kb-web] judge unreachable host=%s (%s)" % (host, str(e)[:60]))
        return None


# 注入到网页里的复核脚本：站点判定是异步补齐时，拿到结论就把页面换成拦截页
_WEB_CHECK_JS = """<script>(function(){
var H=%(host)s, N=0;
function kill(reason){
  try{
    document.querySelectorAll("video,audio").forEach(function(m){try{m.pause();m.src="";}catch(e){}});
  }catch(e){}
  document.documentElement.innerHTML='<body style="font:16px/1.7 system-ui;background:#f6f7f9;padding:40px">'
    +'<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;padding:28px 32px">'
    +'<h3 style="margin:0 0 10px">该网页已被组织策略屏蔽</h3>'
    +'<p style="color:#61666d;font-size:14px">经 opencode 判定为<strong>非知识性内容</strong>，按策略不予访问。</p>'
    +'<div style="background:#f6f7f9;border-left:3px solid #fb7299;padding:8px 12px;margin:14px 0;font-size:14px">拦截原因：'+reason+'</div>'
    +'</div></body>';
}
function poll(){
  if(N++>25) return;
  fetch("/kb-web-verify?host="+encodeURIComponent(H),{credentials:"same-origin"})
   .then(function(r){return r.json()})
   .then(function(j){
     if(j && j.known && !j.knowledge){ kill(j.reason||"非知识性内容"); return; }
     setTimeout(poll,1200);
   }).catch(function(){ setTimeout(poll,1500); });
}
setTimeout(poll,1200);
})();</script>"""


def _web_block_page(host, reason, source):
    return _html(_WEB_BLOCK_HTML, reason=html_escape(reason), source=html_escape(source),
                 host=html_escape(host))


def _inject_web_checker(flow, host):
    if not WEB_INJECT:
        return
    try:
        body = flow.response.content
    except Exception:
        return
    if _WEB_CHECK_MARK in body:
        return
    js = (_WEB_CHECK_JS % {"host": json.dumps(host)}).encode("utf-8")
    idx = body.lower().rfind(b"</body>")
    new_body = body + js if idx == -1 else body[:idx] + js + body[idx:]
    flow.response.content = new_body
    flow.response.headers["content-length"] = str(len(new_body))


def _web_skip(host, reason, quiet=False):
    if not quiet:
        ctx.log.info("[kb-web] skip host=%s reason=%s" % (host, reason))
    return None


def _web_gate(flow, ctype):
    """网页级知识门禁：非知识类网页返回 403"""
    host = (flow.request.pretty_host or "").lower()
    if WEB_GATE_MODE == "off":
        return _web_skip(host, "gate-off")
    req = flow.request
    if req.method != "GET":
        return _web_skip(host, "method", quiet=True)
    dest = (req.headers.get("Sec-Fetch-Dest") or "").lower()
    if dest and dest != "document":
        return _web_skip(host, "dest=%s" % dest, quiet=True)
    if "text/html" not in ctype:
        return _web_skip(host, "ctype=%s" % ctype[:40], quiet=True)
    if not host or _is_local_host(host):
        return _web_skip(host, "local")
    try:
        raw = flow.response.content
    except Exception as e:
        return _web_skip(host, "no-content(%s)" % str(e)[:40])
    title, desc = _extract_title_desc(raw)
    v = _web_verdict(host, title, desc)
    if not v:
        v = {"known": False, "reason": "裁决服务暂不可达"}
    if v.get("known"):
        if v.get("knowledge"):
            return
        ctx.log.info("[kb-web] BLOCK host=%s reason=%s source=%s from=%s"
                     % (host, (v.get("reason") or "")[:40], v.get("source", ""),
                        _client_ip(flow)))
        flow.response = http.Response.make(
            403, _web_block_page(host, v.get("reason") or "非知识性内容", v.get("source", "")),
            {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        return
    # 结论还没出（超时）-> 不放行原文，给"判定中"落地页，拿到结论后再决定放行/拦截
    back = flow.request.pretty_url
    flow.response = http.Response.make(
        200,
        _html(_WEB_VERIFY_HTML, host=html_escape(host), back=json.dumps(back),
              bvid=json.dumps(host)),
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )
    ctx.log.info("[kb-web] VERIFY page host=%s from=%s" % (host, _client_ip(flow)))




# ---------------- 番剧/综艺门禁 ----------------
_PGC_BLOCK_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>该作品已被组织策略屏蔽</title><style>%(css)s</style></head><body>
<div class="box">
<h3>该作品已被组织策略屏蔽</h3>
<p>该番剧/综艺经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予观看。</p>
<div class="reason">拦截原因：%(reason)s</div>
<p>作品：%(title)s ｜ 判定方式：%(source)s</p>
<p style="margin-top:18px"><a href="https://www.bilibili.com/">返回首页</a></p>
</div></body></html>"""


_PGC_VERIFY_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>作品核验中</title><style>%(css)s</style></head><body>
<div class="box">
<h3>作品内容核验中…</h3>
<p>正在通过 opencode 判定该番剧/综艺是否属于知识性内容，请稍候。</p>
<div class="bar"><i></i></div>
<div class="reason" id="rs">作品：%(title)s</div>
</div>
<script>(function(){
var T=%(ptype)s, I=%(pid)s, BACK=%(back)s, N=0, ERRS=0;
function blk(reason){
  document.getElementById("rs").innerHTML="拦截原因："+reason;
  document.querySelector(".bar").style.display="none";
  document.querySelector("h3").textContent="该作品已被组织策略屏蔽";
  document.querySelectorAll("p")[0].innerHTML="经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予观看。";
}
function go(){ try{ location.replace(BACK); }catch(e){ location.href=BACK; } }
function poll(){
  if(N++>40){ go(); return; }
  fetch("/kb-pgc-verify?type="+T+"&id="+encodeURIComponent(I),{credentials:"same-origin"})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j || !j.known){ if(++ERRS>4){ go(); return; } setTimeout(poll,1000); return; }
     if(j.knowledge){ go(); return; }
     blk(j.reason||"非知识性内容");
   }).catch(function(){ if(++ERRS>4){ go(); return; } setTimeout(poll,1200); });
}
setTimeout(poll,600);
})();</script></body></html>"""


def _pgc_verdict(id_type, id_val, wait=1, timeout=None):
    """问裁决服务这部番剧/综艺是不是知识类"""
    qs = urllib.parse.urlencode({"type": id_type, "id": str(id_val), "wait": str(wait),
                                 "maxwait": str(WEB_WAIT)})
    try:
        return _judge_req("/pgc-verdict?" + qs, timeout=timeout or (WEB_WAIT + 3))
    except Exception as e:
        ctx.log.warn("[kb-pgc] judge unreachable %s:%s (%s)" % (id_type, id_val, str(e)[:60]))
        return None


def _gate_pgc(flow, id_type, id_val):
    """番剧/综艺/影视/纪录片 知识性门禁"""
    v = _pgc_verdict(id_type, id_val, wait=1)
    if v and v.get("known"):
        if v.get("knowledge"):
            return
        ctx.log.info("[kb-pgc] BLOCK %s:%s reason=%s source=%s from=%s"
                     % (id_type, id_val, (v.get("reason") or "")[:40], v.get("source", ""),
                        _client_ip(flow)))
        flow.response = http.Response.make(
            403,
            _html(_PGC_BLOCK_HTML, reason=html_escape(v.get("reason") or "非知识性内容"),
                  title=html_escape(v.get("title") or id_val), source=v.get("source", "")),
            {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        return
    try:
        _pgc_verdict(id_type, id_val, wait=0, timeout=1.5)
    except Exception:
        pass
    back = flow.request.pretty_url
    flow.response = http.Response.make(
        200,
        _html(_PGC_VERIFY_HTML, ptype=json.dumps(id_type), pid=json.dumps(str(id_val)),
              title=html_escape(id_val), back=json.dumps(back)),
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )
    ctx.log.info("[kb-pgc] VERIFY page %s:%s from=%s" % (id_type, id_val, _client_ip(flow)))

# ---------------- 直播门禁 ----------------
_LIVE_BLOCK_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>该直播间已被组织策略屏蔽</title><style>%(css)s</style></head><body>
<div class="box">
<h3>该直播间已被组织策略屏蔽</h3>
<p>该直播间经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予观看。</p>
<div class="reason">拦截原因：%(reason)s</div>
<p>直播间：%(room)s ｜ 标题：%(title)s ｜ 判定方式：%(source)s</p>
<p style="margin-top:18px"><a href="https://www.bilibili.com/">返回首页</a></p>
</div></body></html>"""


_LIVE_VERIFY_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>直播核验中</title><style>%(css)s</style></head><body>
<div class="box">
<h3>直播内容核验中…</h3>
<p>正在通过 opencode 判定该直播间是否属于知识性内容，请稍候。</p>
<div class="bar"><i></i></div>
<div class="reason" id="rs">直播间：%(room)s</div>
</div>
<script>(function(){
var R=%(room)s, BACK=%(back)s, N=0, ERRS=0;
function blk(reason){
  document.getElementById("rs").innerHTML="拦截原因："+reason;
  document.querySelector(".bar").style.display="none";
  document.querySelector("h3").textContent="该直播间已被组织策略屏蔽";
  document.querySelectorAll("p")[0].innerHTML="经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予观看。";
}
function go(){ try{ location.replace(BACK); }catch(e){ location.href=BACK; } }
function poll(){
  if(N++>40){ go(); return; }
  fetch("/kb-live-verify?room_id="+encodeURIComponent(R),{credentials:"same-origin"})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j || !j.known){ if(++ERRS>4){ go(); return; } setTimeout(poll,1000); return; }
     if(j.knowledge){ go(); return; }
     blk(j.reason||"非知识性内容");
   }).catch(function(){ if(++ERRS>4){ go(); return; } setTimeout(poll,1200); });
}
setTimeout(poll,600);
})();</script></body></html>"""


def _live_verdict(room_id, wait=1, timeout=None):
    """问裁决服务这个直播间是不是知识类"""
    qs = urllib.parse.urlencode({"room_id": str(room_id), "wait": str(wait),
                                 "maxwait": str(WEB_WAIT)})
    try:
        return _judge_req("/live-verdict?" + qs, timeout=timeout or (WEB_WAIT + 3))
    except Exception as e:
        ctx.log.warn("[kb-live] judge unreachable room=%s (%s)" % (room_id, str(e)[:60]))
        return None


def _gate_live(flow, room_id):
    """直播间知识性门禁：非知识类直播间直接 403"""
    v = _live_verdict(room_id, wait=1)
    if v and v.get("known"):
        if v.get("knowledge"):
            return
        ctx.log.info("[kb-live] BLOCK room=%s reason=%s source=%s from=%s"
                     % (room_id, (v.get("reason") or "")[:40], v.get("source", ""),
                        _client_ip(flow)))
        flow.response = http.Response.make(
            403,
            _html(_LIVE_BLOCK_HTML, reason=html_escape(v.get("reason") or "非知识性内容"),
                  title=html_escape(v.get("title") or room_id), source=v.get("source", ""),
                  room=html_escape(room_id)),
            {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        return
    # 未裁决 -> 触发后台裁决，再给"核验中"落地页
    try:
        _live_verdict(room_id, wait=0, timeout=1.5)
    except Exception:
        pass
    back = flow.request.pretty_url
    flow.response = http.Response.make(
        200,
        _html(_LIVE_VERIFY_HTML, room=json.dumps(str(room_id)), back=json.dumps(back)),
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )
    ctx.log.info("[kb-live] VERIFY page room=%s from=%s" % (room_id, _client_ip(flow)))

# ---------------- 页面模板 ----------------
_CSS = """
body{font:16px/1.7 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f6f7f9;color:#18191c;margin:0;padding:40px}
.box{max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:28px 32px;
box-shadow:0 2px 12px rgba(0,0,0,.06)}
h3{margin:0 0 10px;font-size:20px}
p{margin:6px 0;color:#61666d;font-size:14px}
.bar{height:4px;background:#e8eaed;border-radius:3px;overflow:hidden;margin:18px 0 8px}
.bar i{display:block;height:100%;width:40%;background:#fb7299;border-radius:3px;
animation:sl 1.1s infinite linear}
@keyframes sl{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
.reason{background:#f6f7f9;border-left:3px solid #fb7299;padding:8px 12px;border-radius:4px;
margin:14px 0;font-size:14px}
code{background:#f2f3f5;padding:1px 6px;border-radius:4px;font-size:13px}
a{color:#fb7299}
"""

_VERIFY_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>内容核验中 · Bilibili</title><style>%(css)s</style></head><body>
<div class="box">
<h3>内容核验中…</h3>
<p>正在通过 opencode 判定该视频是否属于知识性内容，请稍候。</p>
<div class="bar"><i></i></div>
<p id="st">状态：等待裁决…</p>
<div class="reason" id="rs" style="display:none"></div>
</div>
<script>
var BVID=%(bvid)s, BACK=%(back)s;
var tries=0, errs=0;
function q(p){return fetch(p,{credentials:"same-origin"}).then(function(r){return r.json();});}
function show(msg){document.getElementById("st").innerText="状态："+msg;}
function block(reason){
  document.title = "该视频已被组织策略屏蔽";
  document.getElementById("st").style.display="none";
  var rs=document.getElementById("rs"); rs.style.display="block";
  rs.textContent="拦截原因："+(reason||"非知识性内容");
  document.querySelector(".bar").style.display="none";
  document.querySelector("h3").textContent="该视频已被组织策略屏蔽";
}
function go(){ window.location.replace(BACK); }
q("/kb-start?bvid="+BVID+"&prio=1").catch(function(){});
var timer=setInterval(function(){
  tries++;
  q("/kb-verify?bvid="+BVID).then(function(v){
    errs=0;
    if(v && v.known){
      clearInterval(timer);
      if(v.knowledge){ document.title="正在打开…"; show("判定为知识性内容，正在打开…"); setTimeout(go,300); }
      else { show("已拦截"); block(v.reason); }
      return;
    }
    show("等待 opencode 裁决… ("+tries+")");
  }).catch(function(e){
    errs++;
    show("裁决服务响应异常 ("+errs+")");
    if(errs>=4){ clearInterval(timer); show("裁决服务不可用，已按规则放行"); setTimeout(go,800); }
  });
},1200);
</script></body></html>"""

_BLOCK_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>该视频已被组织策略屏蔽</title><style>%(css)s</style></head><body>
<div class="box">
<h3>该视频已被组织策略屏蔽</h3>
<p>本视频经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予播放。</p>
<div class="reason">拦截原因：%(reason)s</div>
<p>视频标题：%(title)s</p>
<p>判定方式：%(source)s ｜ 视频 ID：%(bvid)s</p>
<p style="margin-top:18px"><a href="https://www.bilibili.com/">返回首页</a></p>
</div></body></html>"""


_WEB_BLOCK_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>该网页已被组织策略屏蔽</title><style>%(css)s</style></head><body>
<div class="box">
<h3>该网页已被组织策略屏蔽</h3>
<p>该网页经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予访问。</p>
<div class="reason">拦截原因：%(reason)s</div>
<p>站点：%(host)s ｜ 判定方式：%(source)s</p>
</div></body></html>"""

_WEB_CHECK_MARK = b"__kb_web_checker__"

_WEB_VERIFY_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网页核验中</title><style>%(css)s</style></head><body>
<div class="box">
<h3>网页核验中…</h3>
<p>正在通过 opencode 判定该网页是否属于知识性内容，请稍候。</p>
<div class="bar"><i></i></div>
<div class="reason" id="rs">站点：%(host)s</div>
</div>
<script>(function(){
var H=%(bvid)s, BACK=%(back)s, N=0, ERRS=0;
function blk(reason){
  document.getElementById("rs").innerHTML="拦截原因："+reason;
  document.querySelector(".bar").style.display="none";
  document.querySelector("h3").textContent="该网页已被组织策略屏蔽";
  document.querySelectorAll("p")[0].innerHTML="经 opencode 判定为<strong>非知识性内容</strong>，按组织策略不予访问。";
}
function go(){ try{ location.replace(BACK); }catch(e){ location.href=BACK; } }
function poll(){
  if(N++>40){ go(); return; }
  fetch("/kb-web-verify?host="+encodeURIComponent(H),{credentials:"same-origin"})
   .then(function(r){return r.json()})
   .then(function(j){
     if(!j || !j.known){ if(++ERRS>4){ go(); return; } setTimeout(poll,1000); return; }
     if(j.knowledge){ go(); return; }
     blk(j.reason||"非知识性内容");
   }).catch(function(){ if(++ERRS>4){ go(); return; } setTimeout(poll,1200); });
}
setTimeout(poll,600);
})();</script></body></html>"""


def _html(tpl, **kw):
    kw.setdefault("css", _CSS)
    return (tpl % kw).encode("utf-8")


# ---------------- 关键词匹配 ----------------
def _url_has_keyword(url: str) -> bool:
    low = url.lower()
    try:
        decoded = urllib.parse.unquote(low)
    except Exception:
        decoded = low
    for kw in KEYWORDS:
        k = kw.lower()
        if k in low or k in decoded:
            return True
    return False


# ---------------- API JSON 过滤 ----------------
def _is_game_item(d) -> bool:
    """API 条目是否属于游戏分区"""
    for k in ("typename", "tname", "partition", "category_name", "tname_zh"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() == GAME_PARTITION:
            return True
    return False


def _title_of(d) -> str:
    for k in ("title", "arcurl_title", "name"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _is_video_item(d) -> bool:
    return any(k in d for k in ("title", "arcurl", "bvid", "playurl", "goto"))


def _filter_json(node):
    """递归剔除游戏分区 / 关键词命中的视频条目"""
    if isinstance(node, list):
        out = []
        for item in node:
            if (
                isinstance(item, dict)
                and _is_video_item(item)
                and (_is_game_item(item) or KW_RE.search(_title_of(item)))
            ):
                continue
            out.append(_filter_json(item))
        return out
    if isinstance(node, dict):
        return {k: _filter_json(v) for k, v in node.items()}
    return node


def _collect_bvids(node, out):
    """收集 JSON 中的 bvid（用于后台预判）"""
    if isinstance(node, dict):
        b = node.get("bvid")
        if isinstance(b, str) and BVID_RE.fullmatch(b):
            out.append(b)
        for v in node.values():
            _collect_bvids(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_bvids(v, out)


# ---------------- 注入 JS ----------------
JS_TEMPLATE = r"""
<script>(function(){
if (window.__biliKwFilter) return; window.__biliKwFilter = 1;
window.__biliKbHide = __HIDE__;
var KW = __KEYWORDS__;
var RE = new RegExp(KW.join("|"), "i");
function txt(el){
  if(!el) return "";
  return (el.innerText||el.textContent||"") + " " +
         (el.getAttribute&&el.getAttribute("title")||"") + " " +
         (el.getAttribute&&el.getAttribute("aria-label")||"");
}
function isGameCard(el){
  var t = txt(el);
  if (RE.test(t)) return true;
  var badge = el.querySelector && el.querySelector(".bili-type-tag,.card-box .type,.tag-link,.badge");
  if (badge && (badge.innerText||"").trim() === "游戏") return true;
  return false;
}
function hideCards(){
  var anchors = document.querySelectorAll('a[href*="/video/"], a[href*="/bangumi/play"], a[href*="ep."]');
  for (var i=0;i<anchors.length;i++){
    var a = anchors[i];
    var card = a.closest('.video-card,.bili-video-card,.card-box,.feed-card,.video-list-item,.small-item,.item,.bili-album__info--tit,.video-list .item') || a.parentElement;
    if (!card) continue;
    if (isGameCard(card) || isGameCard(a) || (a.parentElement && isGameCard(a.parentElement))) {
      card.setAttribute("data-bili-kw-blocked","1");
      card.style.setProperty("display","none","important");
    }
  }
}
function blockPlayer(){
  if (!/\/video\/|\/bangumi\/play/.test(location.pathname)) return;
  var h1 = document.querySelector('h1[title], h1.video-title, .video-title, .bangumi-title');
  var t = (document.title||"") + " " + (h1? txt(h1) : "");
  var isGamePart = !!document.querySelector('a[href*="rank/game"]');
  if (!RE.test(t) && !isGamePart) return;
  var vids = document.querySelectorAll('video');
  for (var i=0;i<vids.length;i++){ try{ vids[i].pause(); }catch(e){} }
  if (document.getElementById("__bili_kw_notice")) return;
  var d = document.createElement("div");
  d.id = "__bili_kw_notice";
  d.textContent = "该视频已被组织策略屏蔽（游戏相关内容）";
  d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#18191c;color:#fff;font:16px/1.6 system-ui,sans-serif;padding:14px 20px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.4)";
  var mount = function(){ if (document.body) document.body.appendChild(d); else setTimeout(mount, 50); };
  mount();
}
/* ---- 知识性角标：已判定为非知识的卡片加标记（点击将被拦截） ---- */
var known = {};
var requested = {};
function markBad(bvid, reason){
  if (known[bvid]) return;
  known[bvid] = 1;
  var links = document.querySelectorAll('a[href*="/video/'+bvid+'"]');
  for (var i=0;i<links.length;i++){
    var card = links[i].closest('.video-card,.bili-video-card,.card-box,.feed-card,.video-list-item,.small-item,.item')||links[i].parentElement;
    if (!card || card.getAttribute('data-bili-kb-bad')) continue;
    card.setAttribute('data-bili-kb-bad','1');
    card.style.setProperty('opacity','0.45','important');
    if (window.__biliKbHide) { card.style.setProperty('display','none','important'); return; }
    var b = document.createElement('span');
    b.textContent = '非知识·播放将被拦截';
    b.style.cssText='display:inline-block;margin-left:6px;padding:0 6px;border-radius:4px;font-size:12px;line-height:18px;background:#fb7299;color:#fff';
    var t = links[i].getAttribute('title') || links[i].innerText || '';
    card.appendChild(b);
  }
}
function pollVerdicts(){
  var links = document.querySelectorAll('a[href*="/video/"]');
  var want = [], seen = {};
  for (var i=0;i<links.length && want.length<60;i++){
    var m = (links[i].getAttribute('href')||'').match(/BV[0-9A-Za-z]{10}/);
    if (m && !seen[m[0]]){ seen[m[0]]=1; want.push(m[0]); }
  }
  if (!want.length) return;
  var qs = want.map(function(b){return 'bvid='+b;}).join('&');
  fetch('/kb-batch?'+qs,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(j){
    var res = (j && j.results) || {};
    for (var k in res){ if (res[k] && res[k].known && !res[k].knowledge) markBad(k, res[k].reason); }
    applyCats(res);
    // 还没裁决的 -> 触发后台裁决（低优先级），下一轮就有结论
    for (var b2 in seen){
      if (res[b2] || requested[b2]) continue;
      requested[b2] = 1;
      fetch('/kb-start?bvid='+b2+'&prio=0',{credentials:'same-origin'}).catch(function(){});
    }
  }).catch(function(){});
}

/* ---- 学科类推荐：数学/物理 优先，其次 学科/科普/技术/技能 ---- */
var PREF_CATS = {数学:1, 物理:1, 学科:1, 科普:1, 技术:1, 技能:1};
var CAT_ORDER = {数学:-4, 物理:-4, 学科:-3, 科普:-2, 技术:-2, 技能:-2, 资讯:-1};
function catBadgeText(cat){
  if (cat === "数学") return "数学推荐";
  if (cat === "物理") return "物理推荐";
  if (cat === "学科") return "学科推荐";
  return cat + "·知识";
}
function catBadgeColor(cat){
  if (cat === "数学") return "#e02e24";
  if (cat === "物理") return "#ff6a00";
  if (cat === "学科") return "#fb7299";
  return "#00aeec";
}
var onlySubject = false;
try { onlySubject = localStorage.getItem("kb_only_subject") === "1"; } catch(e){}

var CATS = __CATS__;
function quickCat(title){
  var t = (title || "").toLowerCase();
  if (!t) return "";
  for (var c in CATS){
    var ws = CATS[c] || [];
    for (var i=0;i<ws.length;i++){
      var w = (ws[i] || "").toLowerCase();
      if (w && t.indexOf(w) >= 0) return c;
    }
  }
  return "";
}
function scanSubject(){
  var cards = document.querySelectorAll('.feed-card,.video-card,.bili-video-card,.card-box,.video-list-item,.small-item');
  for (var i=0;i<cards.length;i++){
    var c = cards[i];
    if (c.getAttribute('data-kb-cat') !== null) continue;
    var titleEl = c.querySelector('.bili-video-card__info--tit,.video-title,.card-title');
    var title = "";
    if (titleEl){
      title = (titleEl.getAttribute && (titleEl.getAttribute('title') || titleEl.getAttribute('aria-label'))) || (titleEl.innerText || titleEl.textContent || '');
    }
    if (!title){
      var a = c.querySelector('a[href*="/video/"]');
      if (a) title = a.getAttribute('title') || a.innerText || '';
    }
    var cat = quickCat(title);
    c.setAttribute('data-kb-cat', cat || '0');
    if (!cat) continue;
    promote(c, CAT_ORDER[cat] != null ? CAT_ORDER[cat] : -2);
    catBadge(c, catBadgeText(cat), catBadgeColor(cat));
  }
}
/* ---- 学科推荐流：拉知识/科技/科普分区榜单，插到首页顶部 + 悬浮按钮 ---- */
function esc(s){
  return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
        .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
function subjectItemsHTML(items){
  var h = '<div style="display:flex;gap:10px;overflow-x:auto;padding:4px 0 8px;">';
  for (var i=0;i<items.length;i++){
    var it = items[i] || {};
    h += '<a href="/video/'+esc(it.bvid)+'" target="_blank" style="flex:0 0 172px;background:#fff;border-radius:10px;'
       + 'padding:10px 12px;box-shadow:0 2px 10px rgba(0,0,0,.06);text-decoration:none;color:#18191c;">'
       + '<div style="font-size:13px;line-height:1.5;max-height:40px;overflow:hidden;">'+esc(it.title)+'</div>'
       + '<div style="font-size:12px;color:#9499a0;margin-top:6px;">'
       + '<span style="color:'+(it.cat==="数学"?"#e02e24":(it.cat==="物理"?"#ff6a00":"#fb7299"))+';">'
       + esc(it.cat||"学科")+'</span> · '+esc(it.up)+'</div></a>';
  }
  h += '</div>';
  return h;
}
function subjectPanel(items){
  return '<div style="font-size:15px;font-weight:600;margin-bottom:8px;">📚 学科推荐'
       + '<span style="font-size:12px;color:#9499a0;font-weight:400;margin-left:8px;">数学 / 物理 优先，其后为学科·科普·技术（点进去仍按知识门禁判定）</span></div>'
       + subjectItemsHTML(items);
}
var __kbFeedTries = 0;
function loadSubjectFeed(){
  fetch('/kb-subject-feed?limit=16',{credentials:'same-origin'}).then(function(r){return r.json();})
   .then(function(j){
    var items = (j && j.items) || [];
    if (!items.length){
      if (__kbFeedTries++ < 8) setTimeout(loadSubjectFeed, 15000);
      return;
    }
    items.sort(function(a,b){
      var pa = a.prio || 0, pb = b.prio || 0;
      if (pa !== pb) return pb - pa;
      return (b.view || 0) - (a.view || 0);
    });
    // 首页：插到信息流顶部
    var box = document.getElementById('kb-subject-strip');
    if (!box && location.pathname === "/"){
      var grid = document.querySelector('.bili-feed4-layout') || document.querySelector('.bili-feed4');
      if (!grid) return;
      box = document.createElement('div');
      box.id = 'kb-subject-strip';
      box.style.cssText = 'grid-column:1/-1;background:#f6f7f9;border-radius:12px;padding:12px 14px;margin-bottom:4px;';
      grid.insertBefore(box, grid.firstChild);
    }
    if (box) box.innerHTML = subjectPanel(items);
    window.__kbSubjectItems = items;
   }).catch(function(){});
}
function addSubjectButton(){
  if (!document.body) return;
  if (document.getElementById('kb-subject-btn')) return;
  var btn = document.createElement('button');
  btn.id = 'kb-subject-btn';
  btn.textContent = '📚 学科推荐';
  btn.style.cssText = 'position:fixed;left:20px;bottom:110px;z-index:2147483647;background:#00aeec;color:#fff;border:0;border-radius:18px;padding:9px 14px;font:13px/1.4 system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.25)';
  btn.addEventListener('click', function(){
    var panel = document.getElementById('kb-subject-panel');
    if (!panel){
      panel = document.createElement('div');
      panel.id = 'kb-subject-panel';
      panel.style.cssText = 'position:fixed;left:20px;bottom:150px;z-index:2147483646;width:min(920px,92vw);max-height:60vh;overflow:auto;background:#f6f7f9;border-radius:12px;padding:14px 16px;box-shadow:0 6px 24px rgba(0,0,0,.18)';
      panel.innerHTML = '<div style="font-size:13px;color:#9499a0;padding:6px 4px;">加载学科推荐中…</div>';
      document.body.appendChild(panel);
    }
    if (window.__kbSubjectItems && window.__kbSubjectItems.length){
      panel.innerHTML = subjectPanel(window.__kbSubjectItems);
    } else {
      panel.innerHTML = '<div style="font-size:13px;color:#9499a0;">加载学科推荐中…</div>';
      loadSubjectFeed();
      setTimeout(function(){
        if (window.__kbSubjectItems) panel.innerHTML = subjectPanel(window.__kbSubjectItems);
      }, 2500);
    }
  });
  document.body.appendChild(btn);
}
function gridItem(el){
  var p = el;
  for (var i=0;i<4 && p;i++){
    var par = p.parentElement;
    if(!par) break;
    var d = getComputedStyle(par).display;
    if(d === "grid" || d === "flex") return p;
    p = par;
  }
  return el;
}
function cardOf(bvid){
  var links = document.querySelectorAll('a[href*="/video/'+bvid+'"]');
  for (var i=0;i<links.length;i++){
    var c = links[i].closest('.video-card,.bili-video-card,.card-box,.feed-card,.feed-card-container,.video-list-item,.small-item,.item,.bili-album__info--tit');
    if (c) return c;
  }
  return null;
}
function promote(c, ord){
  var o = (ord == null ? -2 : ord);
  var it = gridItem(c);
  if (it) it.style.setProperty("order", String(o), "important");
  c.style.setProperty("order", String(o), "important");
}
function unpromote(c){
  gridItem(c).style.removeProperty("order");
  c.style.removeProperty("order");
}
function catBadge(c, txt, color){
  if (c.querySelector(".kb-cat-badge")) return;
  var b = document.createElement("span");
  b.className = "kb-cat-badge";
  b.textContent = txt;
  b.style.cssText = "display:inline-block;margin-left:6px;padding:0 6px;border-radius:4px;font-size:12px;line-height:18px;color:#fff;background:"+color;
  var t = c.querySelector('.bili-video-card__info--tit,.video-title,.card-title') || c;
  t.appendChild(b);
}
function applyCats(res){
  for (var bvid in res){
    var v = res[bvid];
    if(!v || !v.known) continue;
    var c = cardOf(bvid);
    if(!c) continue;
    var cat = v.cat || "";
    var pref = !!PREF_CATS[cat];
    if (v.knowledge && pref){
      promote(c, CAT_ORDER[cat] != null ? CAT_ORDER[cat] : -2);
      catBadge(c, catBadgeText(cat), catBadgeColor(cat));
    } else if (v.knowledge){
      unpromote(c);
    }
    if (onlySubject){
      var item = gridItem(c);
      if (v.knowledge && pref) item.style.removeProperty("display");
      else item.style.setProperty("display","none","important");
    }
  }
}
function addSubjectToggle(){
  if (!document.body) return;
  if (document.getElementById("kb-subject-toggle")) return;
  var btn = document.createElement("button");
  btn.id = "kb-subject-toggle";
  btn.textContent = onlySubject ? "学科模式:开" : "学科模式:关";
  btn.title = "只显示学科/科普/技术/技能类视频";
  btn.style.cssText = "position:fixed;right:20px;bottom:110px;z-index:2147483647;background:#fb7299;color:#fff;border:0;border-radius:18px;padding:8px 14px;font:13px/1.4 system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.25)";
  btn.addEventListener("click", function(){
    onlySubject = !onlySubject;
    try { localStorage.setItem("kb_only_subject", onlySubject ? "1" : "0"); } catch(e){}
    btn.textContent = onlySubject ? "学科模式:开" : "学科模式:关";
    // 重新取一次结论并按新模式过滤
    var links = document.querySelectorAll('a[href*="/video/"]');
    var want = [], seen = {};
    for (var i=0;i<links.length && want.length<60;i++){
      var m = (links[i].getAttribute('href')||'').match(/BV[0-9A-Za-z]{10}/);
      if (m && !seen[m[0]]){ seen[m[0]]=1; want.push(m[0]); }
    }
    if(!want.length) return;
    fetch('/kb-batch?'+want.map(function(b){return 'bvid='+b;}).join('&'),{credentials:'same-origin'})
      .then(function(r){return r.json();})
      .then(function(j){ applyCats((j && j.results) || {}); })
      .catch(function(){});
  });
  document.body.appendChild(btn);
}
function run(){ try{ hideCards(); blockPlayer(); addSubjectToggle(); addSubjectButton(); scanSubject(); }catch(e){} }
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
new MutationObserver(function(){ if (window.__biliKwT) return; window.__biliKwT = 1; setTimeout(function(){ window.__biliKwT = 0; run(); }, 250); }).observe(document.documentElement, {childList:true, subtree:true});
setTimeout(scanSubject, 1200);
setInterval(scanSubject, 4000);
setTimeout(loadSubjectFeed, 2000);
setInterval(loadSubjectFeed, 1800000);
if (!window.__biliKbTimer){ window.__biliKbTimer = setInterval(pollVerdicts, 3000); setTimeout(pollVerdicts, 1500); }
})();</script>
"""


def _make_js() -> str:
    js = JS_TEMPLATE.replace("__KEYWORDS__", json.dumps(KEYWORDS, ensure_ascii=False))
    js = js.replace("__CATS__", json.dumps({"数学": MATH_WORDS, "物理": PHYSICS_WORDS,
                                             "学科": SUBJECT_WORDS, "技术": TECH_WORDS,
                                             "科普": POP_SCI_WORDS, "技能": SKILL_WORDS},
                                            ensure_ascii=False))
    hide = "1" if os.environ.get("KB_HIDE_NON_KNOWLEDGE", "0") == "1" else "0"
    return js.replace("__HIDE__", hide)


# ---------------- 门禁 ----------------
def _kb_api(flow: http.HTTPFlow, path: str) -> bool:
    """处理浏览器 <-> 裁决服务的内部接口（本地应答，不走上游）"""
    qs = urllib.parse.parse_qs(flow.request.path.split("?")[1] if "?" in flow.request.path else "")
    bvid = (qs.get("bvid") or [""])[0]
    if path == "/kb-verify":
        try:
            data = _judge_req("/verdict?bvid=" + urllib.parse.quote(bvid))
        except Exception:
            data = {"bvid": bvid, "known": False, "error": "judge unreachable"}
    elif path == "/kb-web-verify":
        host = (qs.get("host") or [""])[0].lower()
        try:
            data = _judge_req("/web-verdict?host=" + urllib.parse.quote(host) +
                              "&title=&desc=&wait=0")
        except Exception:
            data = {"host": host, "known": False, "error": "judge unreachable"}
    elif path == "/kb-subject-feed":
        try:
            lim = (qs.get("limit") or ["16"])[0]
            data = _judge_req("/subject-feed?limit=" + urllib.parse.quote(lim))
        except Exception:
            data = {"items": []}
    elif path == "/kb-pgc-verify":
        pid = (qs.get("id") or [""])[0].strip()
        ptype = (qs.get("type") or ["ep"])[0].strip().lower()
        try:
            data = _judge_req("/pgc-verdict?" + urllib.parse.urlencode(
                {"type": ptype, "id": pid, "wait": "0"}))
        except Exception:
            data = {"key": "%s:%s" % (ptype, pid), "known": False, "error": "judge unreachable"}
    elif path == "/kb-live-verify":
        room_id = (qs.get("room_id") or [""])[0].strip()
        try:
            data = _judge_req("/live-verdict?room_id=" + urllib.parse.quote(room_id) + "&wait=0")
        except Exception:
            data = {"room_id": room_id, "known": False, "error": "judge unreachable"}
    elif path == "/kb-start":
        try:
            data = _judge_req("/judge?bvid=" + urllib.parse.quote(bvid) +
                              ("&prio=1" if (qs.get("prio") or ["1"])[0] == "1" else "&prio=0"))
        except Exception:
            data = {"bvid": bvid, "known": False, "error": "judge unreachable"}
    else:  # /kb-batch
        bvids = qs.get("bvid") or []
        try:
            data = _judge_req("/verdicts?" + urllib.parse.urlencode([("bvid", b) for b in bvids[:60]]))
        except Exception:
            data = {"results": {}}
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    flow.response = http.Response.make(
        200, body, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})
    return True


def _gate_video(flow: http.HTTPFlow, bvid: str) -> None:
    """视频页知识性门禁"""
    try:
        v = _judge_req("/verdict?bvid=" + urllib.parse.quote(bvid))
    except Exception as e:
        # 裁决服务不可用 -> 放行（fail-open），仅记录
        ctx.log.warn("[kb-gate] judge unreachable, allow bvid=%s (%s)" % (bvid, str(e)[:60]))
        return
    if v and v.get("known"):
        if v.get("knowledge"):
            return
        ctx.log.info("[kb-gate] BLOCK video bvid=%s reason=%s source=%s from=%s"
                     % (bvid, v.get("reason", "")[:30], v.get("source", ""),
                        _client_ip(flow)))
        flow.response = http.Response.make(
            403,
            _html(_BLOCK_HTML, reason=html_escape(v.get("reason") or "非知识性内容"),
                  title=html_escape(v.get("title") or bvid), source=v.get("source", ""),
                  bvid=bvid),
            {"Content-Type": "text/html; charset=utf-8"},
        )
        return
    # 未裁决 -> 服务端主动触发裁决（不依赖页面 JS），再给核验中落地页
    try:
        _judge_req("/judge?bvid=" + urllib.parse.quote(bvid) + "&prio=1", timeout=1.5)
    except Exception:
        pass
    back = flow.request.pretty_url
    flow.response = http.Response.make(
        200,
        _html(_VERIFY_HTML, bvid=json.dumps(bvid), back=json.dumps(back)),
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )
    ctx.log.info("[kb-gate] VERIFY page bvid=%s" % bvid)


def html_escape(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------- hooks ----------------
def _handle_request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host.lower()
    url = flow.request.pretty_url
    path = flow.request.path

    # PAC 自动配置脚本（内网 Windows 机器用）
    if path == PAC_PATH:
        flow.response = http.Response.make(
            200,
            (PAC_TEMPLATE % {"host": PROXY_HOST, "port": PROXY_PORT,
                             "net": LAN_NET, "mask": LAN_MASK}).encode("utf-8"),
            {"Content-Type": "application/x-ns-proxy-autoconfig"},
        )
        ctx.log.info("[kb-gate] PAC served to %s" % _client_ip(flow))
        return

    # 内部接口（浏览器 <-> 裁决服务）
    for p in KB_API_PREFIX:
        if path.startswith(p):
            if _kb_api(flow, p):
                return

    # 视频页知识性门禁（仅文档导航）
    m = VIDEO_PATH_RE.match(path)
    if m and host.endswith("bilibili.com"):
        dest = (flow.request.headers.get("Sec-Fetch-Dest") or "")
        accept = (flow.request.headers.get("Accept") or "")
        if dest == "document" or "text/html" in accept:
            _gate_video(flow, m.group(1))
            if flow.response is not None:
                return

    # 番剧/综艺/影视/纪录片 知识性门禁（仅文档导航）
    if host.endswith("bilibili.com"):
        dest0 = (flow.request.headers.get("Sec-Fetch-Dest") or "")
        accept0 = (flow.request.headers.get("Accept") or "")
        if dest0 == "document" or "text/html" in accept0:
            m_ep = PGC_EP_RE.match(path)
            m_ss = PGC_SS_RE.match(path)
            m_md = PGC_MD_RE.match(path)
            if m_ep:
                _gate_pgc(flow, "ep", m_ep.group(1))
            elif m_ss:
                _gate_pgc(flow, "ss", m_ss.group(1))
            elif m_md:
                _gate_pgc(flow, "md", m_md.group(1))
            if flow.response is not None:
                return

    # 番剧取流接口：已判定非知识的作品直接断流
    if PGC_PLAYURL_RE.match(path):
        try:
            _qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            for _eid in (_qs.get("ep_id") or [])[:1]:
                ver = None
                try:
                    ver = _pgc_verdict("ep", _eid, wait=0, timeout=2.0)
                except Exception:
                    ver = None
                if ver and ver.get("known") and not ver.get("knowledge"):
                    flow.response = http.Response.make(
                        403, b"blocked by policy",
                        {"Content-Type": "text/plain; charset=utf-8"},
                    )
                    ctx.log.warn("[kb-pgc] BLOCK-PLAY ep=%s reason=%s from=%s"
                                 % (_eid, (ver.get("reason") or "")[:30], _client_ip(flow)))
                    return
        except Exception:
            pass

    # 直播间知识性门禁（live.bilibili.com/<room_id>，仅文档导航）
    if host == "live.bilibili.com" or host.endswith(".live.bilibili.com"):
        m = LIVE_ROOM_RE.match(path.split("?")[0])
        if m:
            dest = (flow.request.headers.get("Sec-Fetch-Dest") or "")
            accept = (flow.request.headers.get("Accept") or "")
            if dest == "document" or "text/html" in accept:
                _gate_live(flow, m.group(1))
                if flow.response is not None:
                    return

    # 直播流：已判定非知识的直播间直接断流（getRoomPlayInfo 是取流地址的接口）
    if "getRoomPlayInfo" in url:
        try:
            _qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            for _rid in (_qs.get("room_id") or [])[:1]:
                ver = None
                try:
                    ver = _live_verdict(_rid, wait=0, timeout=2.0)
                except Exception:
                    ver = None
                if ver and ver.get("known") and not ver.get("knowledge"):
                    flow.response = http.Response.make(
                        403, b"blocked by policy",
                        {"Content-Type": "text/plain; charset=utf-8"},
                    )
                    ctx.log.warn("[kb-live] BLOCK-PLAY room=%s reason=%s from=%s"
                                 % (_rid, (ver.get("reason") or "")[:30], _client_ip(flow)))
                    return
        except Exception:
            pass

    # 游戏区排行等路径
    if GAME_RANK_PATH in url.lower():
        flow.response = http.Response.make(
            403,
            ("<html><head><meta charset='utf-8'></head><body style='font:16px system-ui;padding:40px'>"
             "<h3 style='color:#c00'>该页面已被组织策略屏蔽</h3>"
             "<p>游戏相关内容不可访问。</p></body></html>").encode("utf-8"),
            {"Content-Type": "text/html; charset=utf-8"},
        )
        ctx.log.warn(f"[bili-filter] BLOCK-RANK url={url}")
        return

    # 播放流：已判定非知识的视频直接断流
    # 注意：现代播放器用的是 /x/player/wbi/playurl 且带 avid= 参数，
    #      只匹配 "/x/player/playurl" 会被绕过
    if "playurl" in url.lower():
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        for key in ("bvid", "aid", "avid"):
            for v in qs.get(key, []):
                if v in GAME_BVIDS:
                    flow.response = http.Response.make(
                        403, b"blocked by policy",
                        {"Content-Type": "text/plain; charset=utf-8"},
                    )
                    ctx.log.warn(f"[bili-filter] BLOCK-PLAY {key}={v}")
                    return
                if key == "bvid" and BVID_RE.fullmatch(v or ""):
                    ver = None
                    try:
                        ver = _judge_req("/verdict?bvid=" + urllib.parse.quote(v))
                    except Exception:
                        ver = None
                    if ver and ver.get("known") and not ver.get("knowledge"):
                        flow.response = http.Response.make(
                            403, b"blocked by policy",
                            {"Content-Type": "text/plain; charset=utf-8"},
                        )
                        ctx.log.warn("[kb-gate] BLOCK-PLAY bvid=%s reason=%s from=%s"
                                     % (v, ver.get("reason", "")[:30], _client_ip(flow)))
                        return
                elif key in ("aid", "avid") and str(v).isdigit():
                    try:
                        ver = _judge_req("/verdict?aid=" + urllib.parse.quote(str(v)))
                    except Exception:
                        ver = None
                    if ver and ver.get("known") and not ver.get("knowledge"):
                        flow.response = http.Response.make(
                            403, b"blocked by policy",
                            {"Content-Type": "text/plain; charset=utf-8"},
                        )
                        ctx.log.warn("[kb-gate] BLOCK-PLAY %s=%s reason=%s from=%s"
                                     % (key, v, ver.get("reason", "")[:30], _client_ip(flow)))
                        return

    if any(host == h or host.endswith("." + h.lstrip(".")) for h in BLOCK_URL_HOSTS):
        if _url_has_keyword(url):
            flow.response = http.Response.make(
                403,
                ("<html><head><meta charset='utf-8'></head><body style='font:16px system-ui;padding:40px'>"
                 "<h3 style='color:#c00'>该搜索已被组织策略屏蔽</h3>"
                 "<p>游戏相关内容不可访问。</p></body></html>").encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            )
            ctx.log.warn(f"[bili-filter] BLOCK url={url}")
            return


def _handle_json(flow: http.HTTPFlow, raw: bytes) -> None:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return
    changed = False

    # 记录游戏视频 bvid（view API）
    url = flow.request.pretty_url
    if "/x/web-interface/view" in url:
        d = data.get("data") if isinstance(data, dict) else None
        if isinstance(d, dict) and (d.get("tname") == GAME_PARTITION or KW_RE.search(d.get("title") or "")):
            bvid = d.get("bvid")
            if bvid and bvid not in GAME_BVIDS:
                GAME_BVIDS.add(bvid)
                ctx.log.info(f"[bili-filter] game video bvid={bvid} title={d.get('title','')[:40]}")

    # 后台预判：把流里的 bvid 交给 judge（异步，不阻塞事件循环）
    try:
        bvids = []
        _collect_bvids(data, bvids)
        if bvids:
            _prefetch(bvids[:40])
    except Exception:
        pass

    filtered = _filter_json(data)
    if filtered != data:
        changed = True
        data = filtered

    if changed:
        new_raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        flow.response.content = new_raw
        flow.response.headers["content-length"] = str(len(new_raw))
        ctx.log.info(f"[bili-filter] json filtered {flow.request.pretty_host}{flow.request.path.split('?')[0]} {len(raw)}->{len(new_raw)}")


def _handle_response(flow: http.HTTPFlow) -> None:
    if flow.response is None:
        return
    if WEB_DEBUG and flow.request.pretty_host.lower().endswith("jiemian.com"):
        ctx.log.info("[kb-web] TRACE host=%s status=%s ctype=%s path=%s dest=%s"
                     % (flow.request.pretty_host, flow.response.status_code,
                        flow.response.headers.get("content-type", "")[:40],
                        flow.request.path[:40],
                        flow.request.headers.get("Sec-Fetch-Dest", "")))
    if flow.response.status_code in (403, 444):
        return
    host = flow.request.pretty_host.lower()
    ctype = flow.response.headers.get("content-type", "").lower()
    if flow.request.path.startswith(KB_API_PREFIX):
        return
    # 网页级知识门禁（在 B 站专用逻辑之前，对所有站点生效）
    try:
        _web_gate(flow, ctype)
    except Exception as e:
        ctx.log.warn("[kb-web] gate error host=%s (%s)" % (host, str(e)[:80]))
    if not any(host.endswith(h) for h in INJECT_HOSTS):
        return

    # 0) 预判采集：B站首页 feed 已改走 gaia-gateway(protobuf)，JSON 解析拿不到 bvid，
    #    因此对任意类型响应做字节级 BV 号扫描
    try:
        try:
            raw = flow.response.content
        except Exception:
            raw = b""
        scan_ok = ("json" in ctype or "octet-stream" in ctype or "protobuf" in ctype
                   or "text/plain" in ctype)
        if raw and scan_ok and len(raw) < 4000000:
            found = BVID_BYTES_RE.findall(raw)
            if found:
                _prefetch([f.decode("ascii", "ignore") for f in found[:15]])
    except Exception:
        pass

    # JSON 接口过滤
    if "json" in ctype:
        # res/locs 是首页模块布局配置（分区标题/模块结构），清空会导致首页空白
        if "res/locs" in flow.request.path:
            return
        try:
            raw = flow.response.content
        except Exception:
            return
        _handle_json(flow, raw)
        return

    # HTML 注入
    if "text/html" not in ctype:
        return
    if INJECT_MARKER.encode() in flow.response.content:
        return
    body = flow.response.content
    js = _make_js().encode("utf-8")
    idx = body.lower().rfind(b"</body>")
    new_body = body + js if idx == -1 else body[:idx] + js + body[idx:]
    flow.response.content = new_body
    flow.response.headers["content-length"] = str(len(new_body))
    ctx.log.info(f"[bili-filter] injected host={host} len={len(body)}")




# ---------------- 出口不可达站点（快速失败，避免浏览器看到裸 502）----------------
# 这些域名从本机出口无法访问（境内网络限制），走代理会长时间挂起后 502。
# 命中后直接返回一个说明页；KB_EGRESS_BLOCKED 可按需增删。
EGRESS_BLOCKED = [h.strip().lower() for h in os.environ.get(
    "KB_EGRESS_BLOCKED",
    # Google 系
    "google.com,googleapis.com,gstatic.com,googlesyndication.com,"
    "googleadservices.com,doubleclick.net,googletagmanager.com,google-analytics.com,"
    "googlevideo.com,youtube.com,youtu.be,"
    # 境内出口常见不可达
    "facebook.com,fbcdn.net,instagram.com,twitter.com,x.com,tiktok.com,"
    "telegram.org,t.me,reddit.com,pinterest.com,wikipedia.org,wikimedia.org,"
    "netflix.com,spotify.com,whatsapp.com,dropbox.com,yandex.ru,dailymotion.com,"
    "vimeo.com,zoom.us,discord.com,discordapp.com",
).split(",") if h.strip()]

_unreachable = {}          # host -> 最近一次上游连接失败的时间戳
UNREACHABLE_TTL = 600     # 10 分钟内同一 host 直接快速失败
_FAIL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>出口不可达</title>
<style>body{font:16px/1.8 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f6f7f9;
margin:0;padding:60px 20px;color:#18191c}.card{max-width:620px;margin:0 auto;background:#fff;
border-radius:14px;padding:34px 38px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 14px}code{background:#f2f3f5;padding:2px 6px;border-radius:4px}
.box{background:#f6f7f9;border-left:3px solid #fb7299;padding:10px 14px;border-radius:4px;margin:16px 0}
small{color:#9499a0}</style></head><body><div class="card">
<h1>该站点从本机出口无法访问</h1>
<p>目标站点：<code>%HOST%</code></p>
<p>原因：<b>不是内容门禁拦截</b>，而是这台门禁服务器自身的公网出口无法连接该站点
（境内网络限制/被墙），浏览器通常会显示「502 Bad Gateway」。</p>
<div class="box">处理建议：<br>
&nbsp;1) 该站点若必须访问，请在本机直连（不经过代理）；<br>
&nbsp;2) 或联系管理员在出口侧放行；<br>
&nbsp;3) 本页由门禁代理自动返回，避免长时间等待。</div>
<p><small>reason: %REASON%</small></p>
</div></body></html>"""


def _host_of(flow):
    try:
        return (flow.request.pretty_host or flow.request.host or "").lower()
    except Exception:
        return ""


def _is_egress_blocked(host):
    for h in EGRESS_BLOCKED:
        if host == h or host.endswith("." + h):
            return True
    return False


def _unreachable_now(host):
    ts = _unreachable.get(host)
    return bool(ts) and (time.time() - ts) < UNREACHABLE_TTL


def _fail_fast(flow, host, reason):
    body = _FAIL_HTML.replace("%HOST%", host).replace("%REASON%", reason)
    try:
        flow.response = http.Response.make(
            403, body.encode("utf-8"),
            {"Content-Type": "text/html; charset=utf-8",
             "Cache-Control": "no-store"},
        )
    except Exception:
        pass


def request(flow: http.HTTPFlow) -> None:
    # 出口不可达：直接快速失败（CONNECT 只放行隧道，交给内层请求返回说明页）
    try:
        if flow.request.method != "CONNECT":
            _h = _host_of(flow)
            if _h:
                if _is_egress_blocked(_h):
                    _fail_fast(flow, _h, "egress-blocked")
                    ctx.log.warn("[kb-gate] EGRESS-FAIL host=%s reason=egress-blocked" % _h)
                    return
                if _unreachable_now(_h):
                    _fail_fast(flow, _h, "upstream-unreachable")
                    ctx.log.warn("[kb-gate] EGRESS-FAIL host=%s reason=upstream-unreachable" % _h)
                    return
    except Exception:
        pass
    _handle_request(flow)


def response(flow: http.HTTPFlow) -> None:
    _handle_response(flow)


def error(flow: http.HTTPFlow) -> None:
    """上游连接失败（出口不可达）→ 记住该 host，之后直接快速失败"""
    try:
        if flow.request.method == "CONNECT":
            return
        _h = _host_of(flow)
        if _h:
            _unreachable[_h] = time.time()
            ctx.log.warn("[kb-gate] EGRESS-FAIL host=%s err=%s"
                         % (_h, str(getattr(flow, "error", ""))[:60]))
    except Exception:
        pass


def load(loader):
    ctx.log.info("[kb-gate] addon loaded")
    threading.Thread(target=_pf_worker, daemon=True).start()
