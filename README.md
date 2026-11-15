# claude-code-job-watch

Know when your scheduled Claude Code jobs fail silently.

When you run `claude -p` from launchd or cron, a crash, a timeout or a job that was never loaded
usually means… nothing happens. No mail, no error, you just notice days later.
This repo has two small tools for that:

- **`report_mail.py`** — sends a completion report by SMTP **from the job's shell script**, not from Claude.
  So you still get a mail marked "failed" or "timed out" when `claude -p` crashes or is killed.
  Mails that could not be sent are kept in `unsent/`. Optional one-mail-a-day digest for frequent jobs.
- **`watch/`** — a nightly watchdog. It flags jobs that are not loaded, daemons that died,
  and runs whose report never arrived — and it tells "the Mac was asleep / powered off" apart from a real failure.
  Works with **macOS launchd** and **Linux / WSL cron**.

Python 3 standard library only. No API key, no extra install.
Code comments and mail text are in Japanese.

> Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.

## Quick start

```bash
git clone <this repo> ~/claude-code-job-watch
cd ~/claude-code-job-watch

# 1. Mail settings (never commit the real file)
mkdir -p ~/.config/ai-org-kit
cp mail.conf.example ~/.config/ai-org-kit/mail.conf
chmod 600 ~/.config/ai-org-kit/mail.conf   # then fill in your SMTP (Gmail: app password)

# 2. Dry run — prints the mail instead of sending it
python3 report_mail.py --dry-run send --job "test" --status 0 --summary "hello" --body "ok"
```

Settings can also come from environment variables (`AIORG_MAIL_TO`, `AIORG_SMTP_HOST`, … — see the top of `report_mail.py`).

## Calling it from a job

A minimal job script. The report is sent whatever happens, because it runs from `trap … EXIT`:

```bash
#!/usr/bin/env bash
LOG="$HOME/logs/morning-check-$(date +%F).log"
STATUS=1
trap 'python3 ~/claude-code-job-watch/report_mail.py send --job "Morning check" --status "$STATUS" --log "$LOG"' EXIT

# stop after 30 minutes (exit code 142 is reported as "timed out")
perl -e 'alarm 1800; exec @ARGV' claude -p "$(cat prompt.md)" >"$LOG" 2>&1
STATUS=$?
```

Add `prompt-block.txt` to the end of your prompt. Claude then ends its output with a `=== 完了報告 ===` block,
and `report_mail.py` turns it into the subject (summary) and body. Without the block, the end of the log is used.

## Watchdog

```bash
cp watch/config.example.json watch/config.json   # set your job label prefix (macOS) or cron_jobs (Linux/WSL)
AIORG_MAIL_DRY_RUN=1 bash watch/run.sh            # try it once
```

Run it nightly with `templates/watch.plist` (macOS: replace `__HOME__`, copy to `~/Library/LaunchAgents`,
`launchctl bootstrap gui/$(id -u) <plist>`) or `templates/crontab.example` (Linux/WSL).
Details: [`watch/README.md`](watch/README.md).

## License

MIT — see [LICENSE](LICENSE).

## Want the rest?

These two tools are a small part of **AI Org Kit for Claude Code**: job templates for `claude -p`
(no double runs, timeouts, permission modes), sub-agent definitions, hooks, skills and a learning ledger,
taken from a one-person setup that runs dozens of scheduled jobs every day.
→ https://kit.orecticdesign.com/gh-en

---

## 日本語

Claude Code の定期実行（`claude -p` を launchd・cron で動かす）が、黙って止まったのに気づくための2つの道具です。

- **`report_mail.py`**：Claude ではなく、ジョブのシェルから SMTP で完了報告を送ります。Claude が落ちた回・時間切れの回も「失敗」「打ち切り」で届きます。送れなかった分は `unsent/` に残ります
- **`watch/`**：毎晩の見張り役。読み込まれていない・常駐が落ちた・報告が来ない、を見つけます。Mac が眠っていた・電源が切れていた回は異常に数えません。macOS（launchd）と Linux・WSL（cron）で使えます

Python 3 の標準ライブラリだけで動きます。使い方は上の Quick start と `watch/README.md` を見てください。

この2つは「AI組織キット for Claude Code」（2,980円・買い切り）の一部です。定期実行の雛形（重ねて動かさない・打ち切り・権限の渡し方）・部署（サブエージェント）の定義・フック・スキル・学びの台帳はキットにあります → https://kit.orecticdesign.com/gh

Anthropic の公式ではありません。「Claude」「Claude Code」は Anthropic の商標です。
