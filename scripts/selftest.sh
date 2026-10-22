#!/usr/bin/env bash
# ============================================================================
#  selftest.sh - 门禁自检：不开浏览器，直接用 curl 验证各条判定链
#  用法： bash scripts/selftest.sh
# ============================================================================
set -uo pipefail

PROXY="${KB_PROXY:-http://127.0.0.1:8080}"
JUDGE="${KB_JUDGE_URL:-http://127.0.0.1:8090}"
pass=0; fail=0

ok()   { printf '  \033[1;32m[PASS]\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$1"; fail=$((fail+1)); }
head2() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

# 门禁只对"文档导航"生效（Sec-Fetch-Dest: document / Accept: text/html），
# 所以 curl 必须带上浏览器式请求头，否则会被当作普通 API 请求跳过。
BROWSER_HDRS=(-H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/147.0.0.0 Safari/537.36"
              -H "Sec-Fetch-Dest: document" -H "Accept: text/html,application/xhtml+xml")
code() { curl -s -o /dev/null -w '%{http_code}' -m 20 -x "$PROXY" "${BROWSER_HDRS[@]}" "$1" 2>/dev/null; }

head2 "服务健康"
h=$(curl -s -m 5 "$JUDGE/health"); case "$h" in *'"ok":true'*|*'"ok": true'*) ok "judge /health $h";; *) bad "judge /health $h";; esac
curl -s -m 5 "$JUDGE/stats" | head -c 160; echo

head2 "网页门禁（应拦 / 应放）"
for d in douyin.com 4399.com; do
  c=$(code "https://www.$d/"); [ "$c" = "403" ] && ok "https://www.$d/ -> $c 已拦" || bad "https://www.$d/ -> $c 期望 403"
done
for d in www.gov.cn xueqiu.com; do
  c=$(code "https://$d/"); [ "$c" = "200" ] && ok "https://$d/ -> $c 已放行" || bad "https://$d/ -> $c 期望 200"
done

head2 "视频门禁（用一条知识视频 + 一条游戏视频）"
KNOW="https://www.bilibili.com/video/BV1Xdnc6jEbL/"
GAME="https://www.bilibili.com/video/BV1bwnF6eEYV/"
c=$(code "$KNOW");  [ "$c" = "200" ] && ok "知识视频 -> $c" || bad "知识视频 -> $c 期望 200"
c=$(code "$GAME"); [ "$c" = "403" ] && ok "游戏视频 -> $c 已拦" || bad "游戏视频 -> $c 期望 403"

head2 "直播 / 番剧门禁（娱乐类应拦）"
c=$(code "https://live.bilibili.com/1814032875?hotRank=0"); [ "$c" = "403" ] && ok "娱乐直播间 -> $c" || bad "娱乐直播间 -> $c 期望 403"

head2 "播放流断流（非知识视频）"
c=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -x "$PROXY" \
    -H "User-Agent: Mozilla/5.0 Chrome/147" \
    "https://api.bilibili.com/x/player/wbi/playurl?bvid=BV1bwnF6eEYV&avid=1&cid=1")
[ "$c" = "403" ] && ok "playurl -> $c 已断流" || bad "playurl -> $c 期望 403"

head2 "学科推荐流（数学/物理优先）"
curl -s -m 20 "$JUDGE/subject-feed?limit=8" | python3 -c "
import json,sys
try:
    it=(json.load(sys.stdin) or {}).get('items') or []
except Exception as e:
    print('  解析失败', e); raise SystemExit(1)
if not it:
    print('  [FAIL] 推荐流为空'); raise SystemExit(1)
stem=[x for x in it if x.get('cat') in ('数学','物理')]
print('  items=%d 其中数学/物理=%d' % (len(it), len(stem)))
for x in it[:6]: print('   -', x.get('cat'), (x.get('title') or '')[:34])
raise SystemExit(0 if stem else 2)
" && ok "推荐流含数学/物理" || bad "推荐流未含数学/物理"

printf '\n结果：PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" = "0" ]
