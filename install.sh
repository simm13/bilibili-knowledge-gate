#!/usr/bin/env bash
# ============================================================================
#  Bilibili Knowledge Gate - 一键安装
#  用法： sudo bash install.sh
#  前置：已安装 python3 / mitmproxy / opencode（或按 README 手动装）
# ============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_HOME="${KB_HOME:-/opt/kbjudge}"
PROXY_DIR="${PROXY_DIR:-/opt/mitmproxy}"
CA_DIR="${CA_DIR:-/srv/kbca}"
ENV_FILE="${ENV_FILE:-/etc/kb-gate/gate.env}"

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash install.sh）"

# ---------------------------------------------------------------- 依赖检查
say "检查依赖"
for c in python3 systemctl; do
  command -v "$c" >/dev/null || die "缺少 $c"
done
if ! command -v mitmdump >/dev/null; then
  echo "  ! 未找到 mitmdump：pip install mitmproxy 后重跑本脚本"
fi
if ! command -v opencode >/dev/null; then
  echo "  ! 未找到 opencode：需要 opencode serve 作为 LLM 裁决后端（见 README）"
fi

# ---------------------------------------------------------------- 目录
say "创建目录 $KB_HOME / $PROXY_DIR / $CA_DIR"
mkdir -p "$KB_HOME/audit" "$PROXY_DIR" "$CA_DIR" "$(dirname "$ENV_FILE")"

# ---------------------------------------------------------------- 文件
say "安装裁决服务与规则"
install -m 0755 "$SRC_DIR/judge/judge.py"          "$KB_HOME/judge.py"
install -m 0644 "$SRC_DIR/judge/rules.py"          "$KB_HOME/rules.py"
install -m 0644 "$SRC_DIR/judge/web_rules.py"      "$KB_HOME/web_rules.py"
install -m 0755 "$SRC_DIR/judge/export_audit.py"   "$KB_HOME/export_audit.py"
install -m 0755 "$SRC_DIR/judge/backfill_cat.py"   "$KB_HOME/backfill_cat.py"
[ -f "$KB_HOME/web_allow.txt" ] || : > "$KB_HOME/web_allow.txt"

say "安装 mitmproxy 插件"
install -m 0644 "$SRC_DIR/mitmproxy/bilibili_filter.py" "$PROXY_DIR/bilibili_filter.py"

say "安装客户端辅助文件（CA 下载 / Win11 脚本）"
install -m 0644 "$SRC_DIR/client/README-Windows.txt" "$CA_DIR/README-Windows.txt"
install -m 0644 "$SRC_DIR/client/Test-LanGate.ps1"   "$CA_DIR/Test-LanGate.ps1"
install -m 0644 "$SRC_DIR/client/Fix-CaTrust.ps1"    "$CA_DIR/Fix-CaTrust.ps1"

# ---------------------------------------------------------------- CA
say "生成 mitmproxy CA（客户端要信任它才能解密 HTTPS）"
if [ ! -f "$HOME/.mitmproxy/mitmproxy-ca-cert.cer" ]; then
  # 启动一次 mitmdump 让它生成 CA
  timeout 12 mitmdump -p 127.0.0.1:18099 >/dev/null 2>&1 || true
fi
if [ -f "$HOME/.mitmproxy/mitmproxy-ca-cert.cer" ]; then
  install -m 0644 "$HOME/.mitmproxy/mitmproxy-ca-cert.cer" "$CA_DIR/mitmproxy-ca.cer"
  echo "    CA 已就绪：$CA_DIR/mitmproxy-ca.cer"
else
  echo "    ! 未自动生成 CA，稍后执行：mitmdump -p 18099 然后复制 ~/.mitmproxy/mitmproxy-ca-cert.cer"
fi

# ---------------------------------------------------------------- 环境变量
if [ ! -f "$ENV_FILE" ]; then
  say "生成 $ENV_FILE"
  install -m 0640 "$SRC_DIR/config/gate.env.example" "$ENV_FILE"
fi
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a

# ---------------------------------------------------------------- systemd
say "安装 systemd 单元"
sed -e "s#@KB_HOME@#$KB_HOME#g" -e "s#@PROXY_DIR@#$PROXY_DIR#g" \
    -e "s#@CA_DIR@#$CA_DIR#g" "$SRC_DIR/systemd/mitmfilter.service"   > /etc/systemd/system/mitmfilter.service
sed -e "s#@KB_HOME@#$KB_HOME#g" "$SRC_DIR/systemd/kb-judge.service"     > /etc/systemd/system/kb-judge.service
sed -e "s#@KB_HOME@#$KB_HOME#g" "$SRC_DIR/systemd/oc-judge.service"     > /etc/systemd/system/oc-judge.service
sed -e "s#@CA_DIR@#$CA_DIR#g" -e "s#\${KB_CA_BIND}#${KB_CA_BIND:-0.0.0.0}#g" \
    "$SRC_DIR/systemd/kb-ca-server.service" > /etc/systemd/system/kb-ca-server.service
sed -e "s#@KB_HOME@#$KB_HOME#g" "$SRC_DIR/systemd/kb-audit.service"     > /etc/systemd/system/kb-audit.service
cp "$SRC_DIR/systemd/kb-audit.timer" /etc/systemd/system/kb-audit.timer
systemctl daemon-reload

say "启动服务"
systemctl enable --now kb-ca-server.service
systemctl enable --now oc-judge.service
systemctl enable --now kb-judge.service
systemctl enable --now mitmfilter.service
systemctl enable --now kb-audit.timer

sleep 3
say "服务状态"
for s in kb-ca-server oc-judge kb-judge mitmfilter; do
  printf '  %-16s %s\n' "$s" "$(systemctl is-active $s)"
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

============================================================
 安装完成
============================================================
 代理地址   : http://${IP:-<本机IP>}:${KB_PROXY_PORT:-8080}
 PAC 地址   : http://${IP:-<本机IP>}:${KB_PROXY_PORT:-8080}/proxy.pac
 CA 下载    : http://${IP:-<本机IP>}:${KB_CA_PORT:-8081}/mitmproxy-ca.cer
 一键修证书 : http://${IP:-<本机IP>}:${KB_CA_PORT:-8081}/Fix-CaTrust.ps1
 自检       : bash $SRC_DIR/scripts/selftest.sh
 客户端说明 : $CA_DIR/README-Windows.txt
============================================================
EOF
