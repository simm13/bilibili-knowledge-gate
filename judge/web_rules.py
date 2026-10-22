# -*- coding: utf-8 -*-
"""
web_rules.py - 网页级知识性判定的域名规则表

三层：
  WEB_ALLOW   白名单（工作必需 / 知识工具站）→ 直接放行，不问 LLM
  WEB_BLOCK   黑名单（游戏 / 短视频 / 直播 / 社交八卦 / 小说漫画等娱乐站）→ 直接拦截
  其余        交 LLM 按页面标题+描述判定，结果按 host 缓存

运行时追加白名单：$KB_WEB_ALLOW_FILE（每行一个域名，改完 5 秒内生效，无需重启）
"""
import os
import re
import threading
import time

# ------------------------------------------------------------------ 白名单
WEB_ALLOW = [
    # 搜索引擎 / 浏览器
    "baidu.com", "google.com", "google.com.hk", "googleapis.com", "gstatic.com",
    "bing.com", "sogou.com", "so.com", "duckduckgo.com", "yandex.com", "yandex.ru",
    # 百科 / 词典 / 科普
    "baike.baidu.com", "baike.com", "360baike.com", "wikipedia.org", "wikiwiki.jp",
    "moegirl.icu", "guokr.com", "kepuchina.cn", "cast.org.cn", "sciencenet.cn",
    # 技术文档 / 代码 / 问答
    "developer.mozilla.org", "readthedocs.io", "readthedocs.org", "gitbook.io",
    "gitbook.com", "devdocs.io", "github.com", "githubusercontent.com", "gitlab.com",
    "gitee.com", "codeberg.org", "sourceforge.net", "stackoverflow.com",
    "stackexchange.com", "askubuntu.com", "superuser.com", "serverfault.com",
    "segmentfault.com", "juejin.cn", "csdn.net", "cnblogs.com", "oschina.net",
    "51cto.com", "w3cschool.com", "w3school.com.cn", "runoob.com", "ituring.com.cn",
    "python.org", "pypi.org", "npmjs.com", "npmjs.org", "crates.io", "rubygems.org",
    "packagist.org", "hub.docker.com", "docker.com", "kubernetes.io", "k8s.io",
    "redis.io", "nginx.org", "apache.org", "spring.io", "developer.android.com",
    "developer.apple.com", "learn.microsoft.com", "msdn.microsoft.com", "cloudflare.com",
    "pythonanywhere.com", "replit.com", "codesandbox.io", "colab.research.google.com",
    # 云控制台 / 企业研发工具（工作必需）
    "aws.amazon.com", "amazon.com", "microsoft.com", "azure.microsoft.com", "aliyun.com",
    "alibabacloud.com", "tencent.com", "cloud.tencent.com", "huaweicloud.com",
    "huawei.com", "volcengine.com", "jdcloud.com", "ucloud.cn", "qiniu.com",
    "gitee.com", "gitcode.com", "coding.net", "gitlink.org.cn",
    "jenkins.io", "grafana.net", "kibana.org", "prometheus.io", "sentry.io",
    "sonarcloud.io", "notion.so", "trello.com", "atlassian.net", "jira.com",
    "confluence.com", "redmine.com", "linear.app", "figma.com", "miro.com",
    # 协作 / 办公 / IM
    "feishu.cn", "larkoffice.com", "dingtalk.com", "work.weixin.qq.com", "wx.qq.com",
    "weixin.qq.com", "slack.com", "teams.microsoft.com", "zoom.us", "doc.weixin.qq.com",
    "docs.qq.com", "yuque.com", "shimo.im", "processon.com", "draw.io", "xmind.net",
    "mubu.com", "office.com", "docs.google.com", "drive.google.com", "pan.baidu.com",
    # 学术 / 论文
    "arxiv.org", "cnki.net", "wanfangdata.com.cn", "cqvip.com", "sciencedirect.com",
    "springer.com", "ieee.org", "acm.org", "ncbi.nlm.nih.gov", "nature.com",
    "science.org", "pnas.org", "jstor.org", "scholar.google.com", "semanticscholar.org",
    "researchgate.net", "paperswithcode.com", "huggingface.co", "kaggle.com",
    "openreview.net", "cas.cn", "cnki.com.cn", "sciencechina.cn",
    # 教育 / 考试 / 题库
    "xuetangx.com", "imooc.com", "geekbang.org", "icourse163.org", "study.163.com",
    "nowcoder.com", "leetcode.cn", "leetcode.com", "luogu.com.cn", "chsi.com.cn",
    "neea.edu.cn", "duolingo.com", "gotopedu.cn",
    # 政务 / 公共服务
    "gov.cn", "12306.cn", "kuaidi100.com", "chinatax.gov.cn", "csrc.gov.cn",
    "pbc.gov.cn", "stats.gov.cn", "safe.gov.cn", "nhsa.gov.cn", "12348.gov.cn",
    # 财经 / 行情 / 交易所（量化工作必需）
    "xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "cnr.cn",
    "chinanews.com", "cnstock.com", "stcn.com", "cls.cn", "yicai.com", "caixin.com",
    "21jingji.com", "thepaper.cn", "eastmoney.com", "sina.com.cn", "hexun.com",
    "10jqka.com.cn", "xueqiu.com", "wallstreetcn.com", "ftchinese.com", "reuters.com",
    "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "investing.com",
    "tradingview.com", "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkex.com.hk",
    "hkexnews.hk", "chinabond.com.cn", "chinamoney.com.cn", "jisilu.cn", "wind.com.cn",
    "choice.eastmoney.com", "licai.com.cn", "investor.gov.cn",
    # 技术资讯 / 媒体
    "36kr.com", "infoq.cn", "huxiu.com", "tmtpost.com", "leiphone.com",
    "ifanr.com", "cnbeta.com.tw", "ithome.com", "geekpark.net", "pingwest.com",
    "ruanyifeng.com", "juejin.cn", "segmentfault.com",
    # 实用工具 / 地图 / 生活服务
    "weather.com.cn", "amap.com", "map.baidu.com", "map.qq.com", "ctrip.com",
    "qunar.com", "didi.com", "youdao.com", "processon.com", "chrono.com",
    # AI 工具 / 大模型服务
    "doubao.com", "volces.com", "ark.cn-beijing.volces.com", "coze.cn",
    "chat.openai.com", "chatgpt.com", "claude.ai", "anthropic.com",
    "gemini.google.com", "huggingface.co", "opencode.ai", "kimi.moonshot.cn",
    "moonshot.cn", "zhipuai.cn", "chatglm.cn", "baichuan-ai.com", "minimax.cn",
    "siliconflow.cn", "modelscope.cn", "tongyi.aliyun.com",
    # 学术/教育补充
    "edu.cn", "ac.cn", "org.cn", "gov.cn",
    # 数据源 / 爬虫目标站（量化工作必需，绝不能被门禁误拦）
    # 问财（同花顺 iwencai，DS 任务里用浏览器抓表）、新浪行情接口、搜狐行情、
    # 理杏仁/中证/红利/雪球接口/akshare 文档/数库等
    "iwencai.com", "10jqka.com.cn", "sinajs.cn", "sohu.com", "lixinger.com",
    "csindex.com.cn", "hibor.com.cn", "danjuanfunds.com", "akshare.xyz",
    "legulegu.com", "xueqiu.com", "eastmoney.com", "sina.com.cn",
    "q.stock.sohu.com", "hq.sinajs.cn", "hq.sinajs.cn.cn", "vip.stock.finance.sina.com.cn",
    "datayes.com", "datayes.com.cn", "quant.10jqka.com.cn", "basic.10jqka.com.cn",
    "d.10jqka.com.cn", "news.10jqka.com.cn", "www.solidot.org", "solidot.org",
]

# ------------------------------------------------------------------ 黑名单（娱乐/非知识）
WEB_BLOCK = [
    # 游戏站 / 游戏平台 / 游戏社区
    "4399.com", "7k7k.com", "3839.com", "265g.com", "17173.com", "178.com",
    "gamersky.com", "3dmgame.com", "yxlao.com", "ali213.com", "9game.cn",
    "5173.com", "11.com", "doyo.cn", "taptap.io", "tap.io", "wegame.com",
    "steamcommunity.com", "steampowered.com", "playstation.com", "xbox.com",
    "battle.net", "riotgames.com", "ubisoft.com", "gog.com", "itch.io",
    "epicgames.com", "miyoushe.com", "mihoyo.com", "hoyolab.com", "moyu.com",
    "chuapp.com", "indienova.com", "bahamut.com.tw", "gamer.com.tw", "a9vg.com",
    "duowan.com", "360game.com", "kepai.com", "doyo.cn", "youxia.com",
    # 直播平台
    "douyu.com", "huya.com", "zhibo8.com", "yy.com", "tvocloud.com",
    # 短视频 / 社区 / 影视娱乐
    "douyin.com", "iesdouyin.com", "kuaishou.com", "gifshow.com",
    "xiaohongshu.com", "xhslink.com", "weibo.com", "weibo.cn", "tiktok.com",
    "acfun.cn", "kuaikan.com", "v.qq.com", "iqiyi.com", "youku.com",
    "youku.tv", "mgtv.com", "pptv.com", "1905.com", "mtime.com", "netflix.com",
    "youtube.com", "youtu.be", "twitch.tv", "funshion.com", "letv.com",
    "wasu.tv", "yangshipin.cn", "douban.com", "douban.co",
    # 音乐 / 音频
    "music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "ximalaya.com",
    "spotify.com", "qishui.com", "5sing.cn", "5singk.com", "changba.com",
    # 小说 / 漫画 / 二次元
    "qidian.com", "fanqienovel.com", "ciweimao.com", "zongheng.com", "yuewen.com",
    "dmzj.com", "manhua", "moyuhua", "bangumi.tv", "acg.tv", "hentai", "hanime",
    # 论坛灌水 / 八卦
    "tieba.baidu.com", "hupu.com", "dongqiudi.com", "maopu.com", "jokeji.cn",
    "qsbbs.com", "baozoumi.com", "paonuo.com", "pipixia.com",
    # 消费 / 种草 / 购物
    "taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "yhd.com", "vip.com",
    "smzdm.com", "manmanbuy.com", "gwdang.com", "youzan.com", "meituan.com",
    "dianping.com", "womai.com",
    # 彩票 / 竞猜
    "sporttery.cn", "zhcw.com", "cwl.gov.cn", "500.com", "win007", "310win",
    "nowscore", "leisu.com", "zuqiu8",
]

# 完全豁免：不做网页级判定（自有门禁更细，或属于基础设施/内网）
WEB_EXEMPT = [
    "bilibili.com", "bilivideo.com", "b23.tv", "bilibili.tv",   # 已有视频级门禁
    "hdslb.com", "bilivideo.cn", "bilivideo.com",
]


# 页面标题/描述里的娱乐信号（命中即拦，不等 LLM）
WEB_TITLE_BLOCK = [
    "免费游戏", "在线游戏", "网页游戏", "小游戏", "游戏下载", "游戏解说",
    "游戏实况", "游戏攻略", "游戏直播", "游戏中心", "游戏库", "游戏盒子",
    "游戏下载基地", "开黑", "上分", "皮肤", "抽卡", "开服",
    "直播回放", "直播间", "弹幕", "番剧", "动漫", "漫画", "轻小说",
    "相声", "小品", "综艺", "电视剧", "电影解说", "翻唱", "热舞", "宅舞",
]


def _norm(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _match(host: str, table) -> str:
    """后缀匹配：example.com 命中 example.com 与 a.example.com"""
    h = _norm(host)
    if not h:
        return ""
    for d in table:
        d = _norm(d)
        if not d:
            continue
        if h == d or h.endswith("." + d):
            return d
    return ""


def is_exempt(host: str) -> bool:
    return bool(_match(host, WEB_EXEMPT))


_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def is_local(host: str) -> bool:
    """内网/本机地址：不做网页级判定"""
    h = _norm(host)
    if not h or h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if _IP_RE.match(h):
        try:
            p = [int(x) for x in h.split(".")]
        except Exception:
            return True
        if p[0] in (10, 127, 0):
            return True
        if p[0] == 172 and 16 <= p[1] <= 31:
            return True
        if p[0] == 192 and p[1] == 168:
            return True
        if p[0] == 169 and p[1] == 254:
            return True
        return False
    return h.endswith((".local", ".lan", ".internal", ".home.arpa"))


def lookup(host: str):
    """返回 ('allow'|'block'|'grey', 命中的域名)
    白/黑名单都命中时，取**更具体（更长）**的那个，例如 tieba.baidu.com(黑) 优先于 baidu.com(白)"""
    h = _norm(host)
    if not h or is_local(h):
        return ("allow", "local")
    a = _match(h, WEB_ALLOW)
    b = _match(h, WEB_BLOCK)
    if a and b:
        return ("block", b) if len(b) > len(a) else ("allow", a)
    if a:
        return ("allow", a)
    if b:
        return ("block", b)
    return ("grey", "")


# ------------------------------------------------------------------ 运行时白名单（热加载）
_ALLOW_FILE = os.environ.get("KB_WEB_ALLOW_FILE", "/opt/kbjudge/web_allow.txt")
_al_cache = {"mtime": 0, "items": []}
_al_lock = threading.Lock()


def _load_allow_file():
    try:
        st = os.stat(_ALLOW_FILE)
    except Exception:
        with _al_lock:
            _al_cache["mtime"] = 0
            _al_cache["items"] = []
        return []
    with _al_lock:
        if st.st_mtime == _al_cache["mtime"]:
            return _al_cache["items"]
    items = []
    try:
        with open(_ALLOW_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    items.append(line.lstrip(".").rstrip("."))
    except Exception:
        items = []
    with _al_lock:
        _al_cache["mtime"] = st.st_mtime
        _al_cache["items"] = items
    return items


def title_block(title: str, desc: str = "") -> str:
    """标题/描述里命中娱乐信号则返回命中的词，否则空串"""
    t = ((title or "") + " " + (desc or "")).lower()
    for w in WEB_TITLE_BLOCK:
        if w.lower() in t:
            return w
    return ""


def runtime_allow(host: str) -> str:
    """运行时白名单（web_allow.txt）命中返回域名，否则空串"""
    h = _norm(host)
    for d in _load_allow_file():
        dd = _norm(d)          # 统一去掉 www. 前缀再比较
        if not dd:
            continue
        if h == dd or h.endswith("." + dd):
            return dd
    return ""
