#!/usr/bin/env bash
# ============================================================================
#  healthcheck.sh - 快速看四个服务 + 关键端口 + 今日裁决量
# ============================================================================
set -uo pipefail
JUDGE="${KB_JUDGE_URL:-http://127.0.0.1:8090}"
printf '\n== 服务 ==\n'
for s in mitmfilter kb-judge oc-judge kb-ca-server; do
  printf '  %-16s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null || echo 'not-found')"
done
printf '\n== 端口 ==\n'
ss -lntp 2>/dev/null | grep -E ':(8080|8081|8090|4096)\b' || echo '  (无监听)'
printf '\n== 裁决统计 ==\n'
curl -s -m 5 "$JUDGE/stats" | head -c 400; echo
printf '\n== 最近拦截日志 ==\n'
journalctl -u mitmfilter.service --since "-10 min" --no-pager 2>/dev/null | grep -E "BLOCK" | tail -8
