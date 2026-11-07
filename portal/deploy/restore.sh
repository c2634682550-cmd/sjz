#!/usr/bin/env bash
# 恢复：先停服务，再解包，再起服务。会覆盖当前 portal.db 与 storage。
#   sudo systemctl stop portal
#   sudo /opt/dafors/portal/deploy/restore.sh /var/backups/portal/portal-20261107-030000.zip
#   sudo systemctl start portal
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dafors}"
ARCHIVE="${1:?用法: restore.sh <备份 zip>}"

if [ ! -f "$ARCHIVE" ]; then
  echo "备份文件不存在：$ARCHIVE" >&2
  exit 1
fi

systemctl is-active --quiet portal && {
  echo "portal 仍在运行，请先 systemctl stop portal" >&2
  exit 1
}

cd "$APP_DIR"
cp -a portal/var "portal/var.bak-$(date +%Y%m%d-%H%M%S)"
"$APP_DIR/.venv/bin/python" -m portal.cli restore --archive "$ARCHIVE"
chown -R portal:portal portal/var
echo "恢复完成；已保留旧 var 目录作为回滚点。"
