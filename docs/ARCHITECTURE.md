# 架构说明

## 1. 组件

```
┌──────────────┐   显式代理/PAC    ┌────────────────────┐
│  内网客户端  │ ────────────────▶ │  mitmproxy :8080   │  (mitmfilter.service)
│  Win / Mac   │                   │  bilibili_filter.py│
└──────────────┘                   └─────────┬──────────┘
                                             │ 127.0.0.1:8090
                                             ▼
                                   ┌────────────────────┐
                                   │  judge :8090       │  (kb-judge.service)
                                   │  5 条判定链        │
                                   │  SQLite 缓存       │
                                   └─────────┬──────────┘
                                             │ 127.0.0.1:4096
                                             ▼
                                   ┌────────────────────┐
                                   │ opencode serve     │  (oc-judge.service)
                                   │  LLM 裁决后端       │
                                   └────────────────────┘

kb-ca-server :8081  —— CA / Win11 脚本 / 自检脚本的 HTTP 下载点
kb-audit.timer      —— 每天 23:50 把裁决导出 CSV 供人工复核
```

## 2. 请求流（以「打开一个视频」为例）

1. 浏览器通过代理访问 `https://www.bilibili.com/video/BVxxx`
2. 插件在 `request` 阶段拦截 → 查 judge `/verdict?bvid=`（毫秒级读缓存）
3. 三种情况：
   - **知识** → 放行，响应阶段注入角标/学科排序脚本
   - **非知识** → 403 拦截页（带理由）；同时把 bvid 记入「已判非知识」集合
   - **未裁决** → 返回「判定中」落地页，前端轮询 `/kb-verify`；同时把 bvid 入队后台裁决
4. 若最终判为非知识，`/x/player/playurl` 请求会被直接 403（`blocked by policy`），防绕过
5. 直播/番剧同理，多一条 `getRoomPlayInfo` / `pgc/player/web/playurl` 的断流点

## 3. 五条判定链

| 链 | 覆盖 | 元数据来源 | 缓存表 |
|---|---|---|---|
| 视频 | `/video/BVxxx` | `x/web-interface/view` | `verdicts` |
| 网页 | 任意 host | 响应 `<title>` + `meta description` | `web_verdicts` |
| 直播 | `live.bilibili.com/<room>` | `api.live.bilibili.com/room/v1/Room/get_info` | `live_verdicts` |
| 番剧 | `/bangumi/play/{ep,ss,md}` | `pgc/view/web/season` / `pgc/view/web/media` | `pgc_verdicts` |
| 学科流 | 主动拉取 | 搜索 API(WBI) + 分区排行榜 | `stem_feed.json` / `subject_feed.json` |

每条链的结构一致：**规则预筛 → LLM → 规则兜底 → 落库缓存**。

## 4. 裁决的优先级与并发

- 三个工作队列：`_q_prio`（用户点击，最高）、`_q_llm_prio`、`_q_bg`（首页预判，最低）
- 预判请求带 `prio=0`，避免把用户正在点的视频排在队尾
- 缓存命中即返回（毫秒级），未命中入队，页面显示「判定中」并轮询

## 5. 分类词表（单一事实源）

`judge/rules.py` 同时被裁决服务和 mitmproxy 插件 import：

```
GAME_TITLES / GAME_WORDS / ENTERTAIN_WORDS   -> 非知识
KNOWLEDGE_STRONG                             -> 知识
KNOWLEDGE_WEAK                               -> 只提示，不自动放行
SUBJECT_WORDS/TECH_WORDS/POP_SCI_WORDS/SKILL_WORDS/INFO_WORDS -> cat 细分类目
MATH_WORDS / PHYSICS_WORDS                   -> 数学 / 物理（学科推荐最优先）
```

## 6. 网页门禁的两种模式

- `confident`（默认）：未知站点最多等 `KB_WEB_WAIT` 秒拿 LLM 结论；等不到就放行（fail-open），并记录待复判
- `strict`：拿不到结论就一直显示「判定中」（fail-closed）
- `off`：只按规则，不做 LLM 判定（最快）

## 7. 有界等待（重要）

judge 的 `maxwait` 参数在 `urllib.parse.parse_qs()` 里返回的是 **list**，
必须写 `float((q.get("maxwait") or ["8"])[0])`，
写成 `float(q.get("maxwait")[0])` 在参数缺失时会直接抛异常。
