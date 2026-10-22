#!/usr/bin/env bash
# ============================================================================
#  Bilibili Knowledge Gate - 卸载（保留数据，除非 --purge）
#  用法： sudo bash uninstall.sh [--purge]
# ============================================================================
set -uo pipefail

KB_HOME="${KB_HOME:-/opt/kbjudge}"
PROXY_DIR="${PROXY_DIR:-/opt/mitmproxy}"
CA_DIR="${CA_DIR:-/srv/kbca}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

for s in mitmfilter kb-judge oc-judge kb-ca-server; do
  systemctl disable --now "$s" 2>/dev/null && echo "stopped $s"
done
systemctl disable --now kb-audit.timer 2>/dev/null
rm -f /etc/systemd/system/{mitmfilter,kb-judge,oc-judge,kb-ca-server,kb-audit.service}.{service,timer} \
      /etc/systemd/system/kb-audit.timer
systemctl daemon-reload

rm -f "$PROXY_DIR/bilibili_filter.py"
rm -f "$CA_DIR"/{README-Windows.txt,Test-LanGate.ps1,Fix-CaTrust.ps1}

if [ "$PURGE" = "1" ]; then
  echo "PURGE: 删除 $KB_HOME 与 CA 私钥"
  rm -rf "$KB_HOME" "$CA_DIR/mitmproxy-ca.cer" "$HOME/.mitmproxy"
else
  echo "保留数据：$KB_HOME （含 verdicts.db 审计库）"
fi
echo "done."
