#!/usr/bin/env bash
# 自動実行の見張り役（毎晩1回呼ぶ。macOS は templates/watch.plist、Linux・WSL は templates/crontab.example）
# 各ジョブが止まっていないか・完了報告が届いているかを確かめ、結果を完了報告メールで送る
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOMATION="$(dirname "$DIR")"
LOG_DIR="$DIR/logs"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PYTHONIOENCODING=utf-8

# 2分で打ち切る（timeout があればそれを、無ければ perl の alarm を使う）
if command -v timeout >/dev/null 2>&1; then
  timeout 120 python3 -I "$DIR/watch.py" >"$LOG" 2>&1
else
  perl -e 'alarm 120; exec @ARGV or exit 127' python3 -I "$DIR/watch.py" >"$LOG" 2>&1
fi
STATUS=$?

# watch.py の最後の行が「要旨: …」。無ければ失敗として扱う
LAST="$(tail -n 1 "$LOG")"
case "$LAST" in
  "要旨: "*) SUMMARY="${LAST#要旨: }" ;;
  *) SUMMARY="見張りの実行に失敗（ログを確認）"; [ "$STATUS" -eq 0 ] && STATUS=1 ;;
esac
[ "$STATUS" -ne 0 ] && SUMMARY="見張りの実行に失敗（ログを確認）"

BODY="$(cat "$LOG")
一覧（全ジョブの状態）: $DIR/status.json
ログ: $LOG"

python3 "$AUTOMATION/report_mail.py" send --job "自動実行の見張り" --status "$STATUS" \
  --category "なし" --summary "$SUMMARY" --body "$BODY"

# 30日より古いログは消す
find "$LOG_DIR" -name '*.log' -mtime +30 -delete
exit "$STATUS"
