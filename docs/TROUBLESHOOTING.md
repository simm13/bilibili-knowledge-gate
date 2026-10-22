# 排错手册

## 快速定位表

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 浏览器「连接不是专用连接」 | `journalctl -u mitmfilter \| grep "Client TLS handshake failed"` | 客户端不信任 CA / 系统时间不对 |
| 网页一直「判定中」 | `curl 127.0.0.1:8090/stats`（`llm` 计数为 0 = LLM 通道挂了） | oc-judge 没起 / 模型名不对 / 免费层限制 |
| 视频没被拦 | `journalctl -u mitmfilter \| grep kb-gate` | 规则没命中 → 看 `source=` 是 rule 还是 llm |
| 视频被误拦 | `journalctl -u mitmfilter \| grep "reason="` | 词表误伤 → 调 `rules.py` 后 `backfill_cat.py` |
| 改完规则没生效 | `ps -o lstart= -p $(systemctl show -p MainPID --value mitmfilter)` vs `stat -c %y 文件` | 进程还在跑旧代码 → 再重启一次并验证输出 |
| 学科推荐条空 | `curl 127.0.0.1:8090/subject-feed?limit=8` | 搜索接口被风控，等几分钟；或看 `stem_feed.json` 存量 |
| B 站接口 -352 | — | 风控：UA/频率敏感，代码里已做重试+随机间隔 |
| 爬虫/行情脚本被打断 | `grep "BLOCK host=" mitmproxy 日志` | 目标域名不在白名单 → 加进 `web_allow.txt` |

## 一、证书问题（最高频）

### 定位

```bash
journalctl -u mitmfilter.service --since "-1h" --no-pager \
  | grep "Client TLS handshake failed"
```

输出形如：

```
[192.168.200.183] Client TLS handshake failed. The client does not trust
the proxy's certificate for cn.bing.com (sslv3 alert certificate unknown)
```

`sslv3 alert certificate unknown` = **客户端不信任 CA**（不是服务端问题）。
方括号里的 IP 就是出问题的那台机器。

### 修复（客户端）

```powershell
# 管理员 PowerShell
iwr http://<服务器IP>:8081/Fix-CaTrust.ps1 -OutFile $env:TEMP\Fix-CaTrust.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\Fix-CaTrust.ps1
```

脚本会：提权 → 下载 CA → 装入 `LocalMachine\Root` + `CurrentUser\Root` → 校验指纹
→ 查系统时间/`w32tm /resync` → 查系统代理 → 关浏览器 → 走代理实测。

手工方式：`http://<服务器IP>:8081/mitmproxy-ca.cer` → 安装证书 → **本地计算机** → 受信任的根证书颁发机构。

> 装完**必须完全退出浏览器**（不是只关标签页），浏览器才会重读信任库。

### 常见变体

- 装了还是报错 → 证书装到了「个人」存储且浏览器只读「本地计算机」→ 重装到本地计算机
- 只有部分网站报错 → 那些站点用了 HSTS 预加载 + CT 强制（`edge://policy` 看证书策略）
- 时间不对 → 证书还没到生效时间，脚本里已自动 `w32tm /resync`

## 二、浏览器报「502 Bad Gateway」

先分清是**谁的 502**：

```bash
journalctl -u mitmfilter.service --since "-10 min" | grep -A2 "502 Bad Gateway"
```

- 日志里是 `CONNECT www.google.com:443` 之类 → **出口不可达**（不是门禁拦截）：
  这些域名从服务器公网出口就连不上。已由 `KB_EGRESS_BLOCKED` 快速失败并返回说明页；
  需要访问就本机直连，或让管理员在出口侧放行。
- 日志里是别的域名 → 看下一行 `error establishing server connection` 的原因。

> 注意：mitmproxy 默认 `connection_strategy=eager` 会在 CONNECT 阶段就连上游，
> 上游不通时浏览器只看到裸 502。本项目用 `lazy`，才能在内层请求返回友好页面。

## 三、LLM 通道

```bash
systemctl status oc-judge
curl -s 127.0.0.1:4096/ 2>/dev/null | head -c 100
journalctl -u kb-judge --since "-10 min" | grep -E "llm model=.*failed|unparsable"
```

常见报错与含义：

| 报错 | 含义 |
|---|---|
| `FreeTierError` / 403 | 免费层不允许带 `tools` 或自定义 agent → 只能用默认 agent + per-call system |
| `Endpoint is unavailable` | 上游模型临时不可用 → 模型链会自动降级到下一个 |
| `timed out` | 首次拉取/冷启动慢 → 提高 `KB_LLM_TIMEOUT` |
| `unparsable` | 模型输出不是 `YES/NO` 开头 → 换模型或调提示词 |

模型名会随 opencode 版本变化，`KB_MODELS` 里的名字要对齐 `opencode models` 的输出。

## 四、用 curl 自测时门禁"不生效"

门禁只对**文档导航**生效（`Sec-Fetch-Dest: document` 或 `Accept: text/html`），
普通 `curl`（`Accept: */*`）会被当成 API 请求直接放行。测试时必须带浏览器头：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -x http://127.0.0.1:8080 \
  -H "User-Agent: Mozilla/5.0 Chrome/147" -H "Sec-Fetch-Dest: document" \
  "https://www.bilibili.com/video/BVxxxxxxxxxx/"
```

（`scripts/selftest.sh` 已经带了这些头。）

## 五、判定不生效

```bash
# 看某个 bvid 的裁决
sqlite3 /opt/kbjudge/verdicts.db \
  "select bvid,knowledge,reason,source,model,title from verdicts where bvid='BVxxx';"
# 重新裁决：删掉这行，它会被重新入队
sqlite3 /opt/kbjudge/verdicts.db "delete from verdicts where bvid='BVxxx';"
```

- `source=rule:*` → 规则层判的，改词表
- `source=llm` → 模型判的，看 `model=` 是哪个模型，调提示词或换模型
- 没有记录 → 从没判过，检查队列 `curl 127.0.0.1:8090/stats`（`queue_prio` / `queue_bg`）

## 六、web 门禁误伤

```bash
# 看某个域名为什么被拦
journalctl -u mitmfilter | grep "kb-web" | grep "yourhost"
# 输出：BLOCK host=xxx reason=... source=rule:web:block
```

- 数据源/行情域名被拦 → 追加到 `/opt/kbjudge/web_allow.txt`（5 秒热生效）
- 放行页出现「判定中」横幅 → 说明该域名不在白名单、走了 LLM 判定；确认后加白名单

## 七、资源与性能

```bash
free -g; df -h /; ps -eo pid,rss,args --sort=-rss | head
```

- **headless 浏览器残留会吃满内存**（一次事故 317 Chrome + 12 Firefox ≈ 42GB RSS）。
  验证脚本必须显式关浏览器；排查用 `pgrep -f "profil[e] /tmp/"`（`[]` 防止匹配到自己）。
- **别误杀桌面用户的浏览器**：`ps -eo pid,args | grep firefox` 若父进程是 `gnome-shell`，
  说明是有人登录在用的桌面浏览器，不要动。
- 磁盘：mitmproxy 的 CA 目录、裁决库、审计 CSV 会慢慢涨，配合 logrotate/定期清理。

## 八、部署后必做验证

```bash
bash scripts/selftest.sh
```

覆盖：judge 存活、网页门禁 2 拦 2 放、视频门禁 1 放 1 拦、直播拦截、playurl 断流、推荐流含数学/物理。
