#!/usr/bin/env bash
# 本机定时兜底：把签到任务写入 crontab（北京时间 09:10 / 20:10）
# 重复执行不会产生重复条目。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARK="# workbuddy-checkin"
PY_BIN="$(command -v python3 || command -v python || true)"

if [ -z "$PY_BIN" ]; then
  echo "[error] 未找到 python3 / python，请先安装 Python 3.9+" >&2
  exit 1
fi

mkdir -p "$DIR/state"
chmod 700 "$DIR/state" 2>/dev/null || true

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# 保留非本任务的既有 crontab 条目
crontab -l 2>/dev/null | grep -v "$MARK" > "$TMP" || true

cat >> "$TMP" <<EOF
10 9 * * * cd "$DIR" && "$PY_BIN" checkin.py --account 1 >> "$DIR/state/cron.out" 2>&1; "$PY_BIN" checkin.py --account 2 >> "$DIR/state/cron.out" 2>&1 $MARK
10 20 * * * cd "$DIR" && "$PY_BIN" checkin.py --account 1 >> "$DIR/state/cron.out" 2>&1; "$PY_BIN" checkin.py --account 2 >> "$DIR/state/cron.out" 2>&1 $MARK
EOF

crontab "$TMP"

echo "[ok] 已安装本机定时任务（去重后共 2 条）："
crontab -l | grep "$MARK"
echo
echo "提示：crontab 使用系统本地时区。若系统不是 Asia/Shanghai，"
echo "      请把上面的 09:10 / 20:10 换算成系统时区后再确认。"
echo "      查看当前时区: date +%Z"
