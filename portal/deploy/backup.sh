#!/usr/bin/env bash
# 每日备份：cron 里 0 3 * * * /opt/dafors/portal/deploy/backup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dafors}"
DEST="${DEST:-/var/backups/portal}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$DEST"
cd "$APP_DIR"
ARCHIVE="$DEST/portal-$(date +%Y%m%d-%H%M%S).zip"

# 内含 portal.db（sqlite backup API 复制的一致性快照）+ secret_key +
# card_pepper + storage（版本包与批次清单），恢复时三者缺一不可。
"$APP_DIR/.venv/bin/python" -m portal.cli backup --out "$ARCHIVE"

# 旧备份滚动删除
find "$DEST" -name 'portal-*.zip' -type f -mtime +"$KEEP_DAYS" -delete
echo "备份完成：$ARCHIVE"
