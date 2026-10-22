# Bilibili Knowledge Gate · B 站「知识性内容」门禁

在 mitmproxy 里加一道**内容准入门禁**：打开视频/网页/直播/番剧时，先由裁决服务判断这是不是**知识性内容**——是则放行，否则 403 拦截（播放流同时断流，防止绕过）。同时在首页做**学科推荐**（数学/物理 优先）。

> 设计目标：把 B 站从"娱乐feed"改造成"学习feed"，而不是简单地屏蔽几个游戏站。

```
浏览器 ──HTTP(S)──▶ mitmproxy :8080 ──▶ 裁决服务 judge :8090 ──▶ opencode serve :4096 (LLM)
                       │                        │
                       │ 注入/拦截/断流          └─ SQLite 缓存 + 每日 CSV 审计
                       └─▶ 学科推荐流（数学/物理优先，首页顶部横排 + 悬浮面板）
```

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [客户端设置（Windows / macOS / Linux）](#客户端设置)
- [配置](#配置)
- [策略：什么算「知识」](#策略什么算知识)
- [学科推荐](#学科推荐)
- [接口一览](#接口一览)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [许可](#许可)

---

## 特性

| 能力 | 说明 |
|---|---|
| 视频门禁 | `/video/BVxxx` 打开即判定；非知识 → 403 拦截页 + `playurl` 断流 |
| 网页门禁 | 未知站点 → 标题/描述判定 → 必要时 LLM 判定，命中娱乐黑名单即 403 |
| 直播门禁 | 直播间页 + `getRoomPlayInfo` 取流双拦截；娱乐分区直接拦 |
| 番剧门禁 | 番剧/综艺/影视/纪录片（`/bangumi/play/*`）判定 + `pgc/player/web/playurl` 断流 |
| 零延迟预判 | 首页/搜索流里的卡片异步上报，裁决完成后角标 + 置顶，点击零等待 |
| 学科推荐 | 首页顶部插入「📚 学科推荐」横排 + 左下悬浮面板，**数学/物理 优先** |
| 只看学科 | 右下角开关，一键隐藏非学科类卡片（本地记住） |
| 审计 | 每个裁决都落库（bvid → 结论/理由/来源/模型），每天 23:50 导出 CSV |
| 降级 | 模型不可用 → 规则兜底并记录，绝不"因为模型挂了就放行娱乐内容" |

---

## 快速开始

### 1. 依赖

```bash
# Debian/Ubuntu
sudo apt install -y python3 python3-pip
pip3 install mitmproxy          # 或 pipx / venv 安装

# LLM 裁决后端：opencode（免费模型即可）
npm i -g opencode-ai             # 或按 https://opencode.ai 安装
opencode models                  # 看看有哪些可用模型
```

> 裁决服务通过 `opencode serve --port 4096 --hostname 127.0.0.1` 调用 LLM。
> 免费层限制：只能使用**默认 agent + per-call system**，不能带 `tools` 或自定义 agent。

### 2. 安装

```bash
git clone <this-repo> bilibili-knowledge-gate
cd bilibili-knowledge-gate
sudo bash install.sh
```

`install.sh` 会：建目录 → 装 4 个服务（`mitmfilter` / `kb-judge` / `oc-judge` / `kb-ca-server`）→ 生成 mitmproxy CA → 写 `/etc/kb-gate/gate.env` → 输出代理/PAC/CA 地址。

### 3. 自检

```bash
bash scripts/selftest.sh     # 不开浏览器，直接验证 5 条判定链
bash scripts/healthcheck.sh  # 看服务状态、端口、今日裁决量
```

### 4. 客户端接入

见下一节。

---

## 客户端设置

### PAC 直连名单（不经过门禁、不做内容策略）

默认直连：内网本地地址 + **GitHub 系**（github.com / githubusercontent.com / githubassets.com /
api.github.com / codeload / avatars / gist / raw / github.io …）+ gitlab / gitee / bitbucket。

- PAC 客户端：自动生效，改完 `systemctl restart mitmfilter`，浏览器里刷新一次 PAC 即可
  （Chrome：`chrome://net-internals/#proxy` → Re-apply settings）
- 手动代理客户端：在 Windows 代理设置的「例外列表」填
  `*.github.com;*.githubusercontent.com;*.githubassets.com;*.github.io;*.githubcopilot.com;*.gitlab.com;*.gitee.com`

> 直连的前提是**客户端自己的线路能出墙**。若某台机器直连 GitHub 会 `connect` 超时，
> 把它从 `KB_PAC_BYPASS` 里去掉即可（改完重启 mitmfilter）。

### Windows 11 / 10

1. **信任 CA**（否则 HTTPS 全是「您的连接不是专用连接」）
   - 一键：管理员 PowerShell 执行
     ```powershell
     iwr http://<服务器IP>:8081/Fix-CaTrust.ps1 -OutFile $env:TEMP\Fix-CaTrust.ps1
     powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\Fix-CaTrust.ps1
     ```
   - 手工：浏览器打开 `http://<服务器IP>:8081/mitmproxy-ca.cer` →「安装证书」→ 存储位置选 **本地计算机** → 「受信任的根证书颁发机构」→ 完成后**完全退出浏览器**再打开。
2. **设代理**：设置 → 网络和 Internet → 代理 → 手动代理
   - 地址 `<服务器IP>`，端口 `8080`；或用自动配置脚本 `http://<服务器IP>:8080/proxy.pac`
3. **自检**：运行 `client/Test-LanGate.ps1`（改脚本顶部的服务器 IP），期望 5 项全 PASS。

### macOS

```bash
# 信任 CA
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain mitmproxy-ca.cer
# 代理：系统设置 → 网络 → 详细信息 → 代理
```

### Linux

```bash
sudo cp mitmproxy-ca.cer /usr/local/share/ca-certificates/mitmproxy.crt
sudo update-ca-certificates
# 浏览器/终端：export http_proxy=http://<服务器IP>:8080 https_proxy=http://<服务器IP>:8080
```

---

## 配置

所有可调项在 `/etc/kb-gate/gate.env`（模板见 `config/gate.env.example`），改完 `systemctl restart mitmfilter kb-judge`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_PROXY_PORT` | 8080 | 代理端口 |
| `KB_PROXY_HOST` | 自动探测 | PAC 里写的对外 IP（多网卡时手动指定） |
| `KB_WEB_GATE` | `confident` | 网页门禁模式：`off` / `confident` / `strict` |
| `KB_WEB_WAIT` | 4 | 未知站点最多等待 LLM 的秒数 |
| `KB_WEB_INJECT` | 1 | 是否注入「判定中」提示条 |
| `KB_MODELS` | 见模板 | LLM 模型链（逗号分隔，按顺序降级） |
| `KB_LLM_DEADLINE` | 70 | 单次裁决整体截止时间（秒） |
| `KB_HIDE_NON_KNOWLEDGE` | 0 | 1 = 首页直接隐藏非知识类卡片 |
| `KB_EGRESS_BLOCKED` | 见 `systemd/mitmfilter.service` | 出口连不上的域名 → 快速失败并给说明页（不是拦截） |
| `KB_WEB_ALLOW_FILE` | `/opt/kbjudge/web_allow.txt` | 运行时白名单（每行一域名，**5 秒内生效，不用重启**） |

---

## 出口不可达（502 Bad Gateway）怎么处理

有些站点（Google、YouTube、Facebook…）从**服务器自己的公网出口**就连不上，
浏览器经代理访问时 mitmproxy 会挂起几十秒，最后弹一个裸的「502 Bad Gateway」。

本项目的处理：

- `KB_EGRESS_BLOCKED` 列出这类域名 → 代理**立刻**返回一个「出口不可达」说明页（不是门禁拦截，页面里写清楚了）
- 运行期发现某个 host 上游连不上 → 记入内存缓存（10 分钟），之后该域名直接快速失败
- 代理用 `--set connection_strategy=lazy`：先建隧道再连上游，才能在内层请求里返回友好页面

```bash
# 改完重启
systemctl restart mitmfilter
# 验证
curl -s -x http://127.0.0.1:8080 -H "Sec-Fetch-Dest: document" https://www.google.com/ | grep -o "<title>.*</title>"
# -> <title>出口不可达</title>
```

## 策略：什么算「知识」

判定链：**规则预筛 → LLM 判定 → 规则兜底**（模型挂了也不放行娱乐内容）。

- **知识（放行）**：教程、科普、编程、学科教学、纪录片、语言学习、考证、行业分析
- **非知识（拦截）**：游戏实况/攻略、游戏直播、娱乐综艺、生活 vlog、搞笑、鬼畜、舞蹈、吃播

规则分五层（`judge/rules.py`）：

```
GAME_TITLES → 知识强白名单 → 娱乐黑名单 → 泛游戏词 → LLM
```

- 命中**具体游戏名**（`GAME_TITLE_RE`）→ 立即判非知识
- 命中**知识强白名单**（`KNOWLEDGE_STRONG_RE`）→ 立即判知识
- 命中**娱乐黑名单 / 泛游戏词** → 判非知识
- 都不命中 → 交 LLM（`LLM_PROMPT`）；LLM 不可用 → 弱知识词只作提示，不自动放行

调词表：编辑 `judge/rules.py` → `systemctl restart mitmfilter kb-judge`。
加白名单网址：往 `web_allow.txt` 追加一行域名，**无需重启**。

---

## 学科推荐

三层叠加：

1. **服务端结论驱动** —— 裁决结果带 `cat` 字段（`数学/物理/学科/科普/技术/技能/资讯/其他`），
   首页命中 `PREF_CATS` 的卡片会 `order:-1` 提到最前 + 打角标。
2. **客户端本地打标** —— 标题命中学科词表即刻打标置顶，不等 LLM（`quickCat()` + `scanSubject()`）。
3. **主动喂内容** —— 裁决服务用 **B 站搜索 API（WBI 签名）** 拉数学/物理教学视频，
   加上知识/科技等分区排行榜，合并去重后按类目优先级排序，注入首页顶部横排 + 悬浮面板。

优先级：`数学 6 > 物理 6 > 学科 5 > 科普/技术/技能 4 > 资讯 2`；
首页排序 CSS `order`：数学/物理 `-4`，学科 `-3`，科普/技术/技能 `-2`。

> 搜索接口限频很严，代码里关键词之间 sleep 3~5s，并做 2 次重试；
> 结果持久化在 `stem_feed.json`，风控期间不会空窗。

---

## 接口一览

裁决服务（`127.0.0.1:8090`，插件内部调用）：

| 端点 | 说明 |
|---|---|
| `GET /health` | 存活探测 |
| `GET /verdict?bvid=BVxxx` | 查裁决结论（只读缓存，毫秒级） |
| `GET /verdicts?bvid=a&bvid=b` | 批量查 |
| `GET /judge?bvid=BVxxx&prio=0` | 触发裁决（入队立即返回，`prio=0` 走低优先级） |
| `GET /web-verdict?host=xxx&wait=&maxwait=` | 网页判定（有界等待） |
| `GET /live-verdict?room_id=&wait=&maxwait=` | 直播判定 |
| `GET /pgc-verdict?type=ep\|ss\|md&id=&wait=&maxwait=` | 番剧判定 |
| `GET /subject-feed?limit=N` | 学科推荐流（数学/物理优先） |
| `GET /stats` / `GET /audit?limit=50` | 统计 / 最近裁决 |

浏览器 ↔ 插件的内部接口（走代理访问 `http://<服务器IP>:8080/kb-*`）：
`/kb-verify` `/kb-start` `/kb-batch` `/kb-web-verify` `/kb-live-verify` `/kb-pgc-verify` `/kb-subject-feed`

---

## 目录结构

```
bilibili-knowledge-gate/
├── install.sh / uninstall.sh      一键装 / 卸
├── config/gate.env.example        全部可调参数
├── judge/
│   ├── judge.py                   裁决服务（5 条判定链 + 缓存 + 审计）
│   ├── rules.py                   词表（单一事实源，插件与裁决共用）
│   ├── web_rules.py               网页白/黑/豁免 + 标题规则
│   ├── export_audit.py            裁决导出 CSV
│   └── backfill_cat.py            类目字段回填
├── mitmproxy/bilibili_filter.py   门禁插件（拦截/注入/断流/推荐）
├── systemd/                       4 个服务 + 1 个审计定时器
├── client/                        Windows 自检与修证书脚本
├── scripts/selftest.sh            5 条判定链自检
├── scripts/healthcheck.sh         服务/端口/统计速查
└── docs/                          架构、策略、排错
```

---

## 常见问题

**Q：浏览器一直提示「您的连接不是专用连接」**
A：客户端没信任 CA。跑 `client/Fix-CaTrust.ps1`（管理员）。
服务器侧确认：`journalctl -u mitmfilter | grep "Client TLS handshake failed"`，
会直接打出是哪台机器（`sslv3 alert certificate unknown` = 不信任 CA）。
**系统时间不对也会报同样的错**，脚本里已自动检查并 `w32tm /resync`。

**Q：网页一直显示「判定中」**
A：LLM 后端没起来或模型名不对。`systemctl status oc-judge`；
`curl 127.0.0.1:8090/stats` 看 `llm` 裁决计数是否为 0；
用 `opencode models` 核对 `KB_MODELS` 里的模型名（版本不同模型名会变）。

**Q：B 站接口返回 -352**
A：B 站风控。搜索/排行类接口对 UA 和频率敏感，代码里已做双 UA + 重试 + 随机间隔；
`-352` 通常是瞬时的，稍后重试即可。

**Q：改了规则没生效**
A：`systemctl restart mitmfilter kb-judge`（网页白名单 `web_allow.txt` 不用重启，5 秒热加载）。
**注意**：覆盖文件后如果行为没变，多半是进程还在跑旧代码——再重启一次并验证输出。

**Q：首页学科推荐条是空的**
A：`curl 127.0.0.1:8090/subject-feed?limit=8` 看返回；空 = 搜索接口被风控，
等几分钟或看 `stem_feed.json` 是否有存量。

**Q：怎么临时关掉门禁**
A：`systemctl stop mitmfilter`（裁决服务可留着，审计继续记）。

更多见 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)。

---

## 安全说明

- **仓库里不包含任何 CA 私钥**：CA 在目标机上由 mitmproxy 现场生成，只把 `.cer`（公钥）分发给客户端。
- 仓库里**不含**任何密码、API Key、内网 IP 以外的真实凭据；配置全走环境变量。
- 门禁只在**内网**使用：代理监听内网地址，客户端装的是**私有 CA**，请勿把 8080 端口暴露到公网。
- 审计库 `verdicts.db` 记录了观看历史（bvid + 标题），注意隐私，按需定期清理。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
