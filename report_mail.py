#!/usr/bin/env python3
"""自動実行の完了報告メール（共通スクリプト）

自動実行（launchd などで無人で動くジョブ）が終わるたびに、結果をメールで知らせる。
Claude に送らせるのではなく、実行スクリプトの側から SMTP で直接送る。
こうすると Claude が途中で落ちた回・時間切れで打ち切られた回も「失敗」「打ち切り」として報告される（送れなかった分は unsent/ に残る）。

Python 3 の標準ライブラリだけで動く（追加のインストールは要らない）。

設定（宛先と SMTP）は、環境変数か設定ファイルで渡す。鍵（パスワード）はこのファイルに書かない。
  設定ファイル: 既定は ~/.config/ai-org-kit/mail.conf（AIORG_MAIL_CONF で場所を変えられる）
               書式は同じフォルダの mail.conf.example を見る。chmod 600 にしておく
  環境変数（設定ファイルより優先）:
    AIORG_MAIL_TO        宛先
    AIORG_MAIL_FROM      送信元（省略時は AIORG_SMTP_USER）
    AIORG_SMTP_HOST      SMTP サーバー
    AIORG_SMTP_PORT      ポート（465 なら SSL、587 なら STARTTLS）
    AIORG_SMTP_USER      ログインの名前
    AIORG_SMTP_PASSWORD  パスワード（Gmail ならアプリパスワード）
    AIORG_MAIL_DRY_RUN   1 にすると送らずに画面へ出す（--dry-run と同じ）

使い方:
  # Claude を呼ぶジョブ: ログの最後にある「完了報告」の囲みから件名・本文を作る
  report_mail.py send --job "毎朝の見回り" --status $STATUS --log $LOG
  # スクリプトだけのジョブ: 区分・要旨・本文を自分で渡す
  report_mail.py send --job "バックアップ" --status 0 --category なし --summary "3件を保存" --body "…"
  # 1日1通のまとめ（何度も動くジョブ向け）。失敗の回はまとめを待たずにすぐ送る
  report_mail.py digest-add --digest hourly --job "毎時の取り込み" --status 0 --body "…"
  report_mail.py digest-send --digest hourly --job "毎時の取り込み 1日のまとめ"
  # 件名・本文を整えずにそのまま送る
  report_mail.py raw --subject "…" --body "…"
  # 送らずに中身だけ確かめる（最初の動作確認に使う）
  report_mail.py --dry-run send --job "テスト" --status 0 --summary "動作確認" --body "本文"

Claude を呼ぶジョブは、最終出力の最後に次の囲みを書かせる（prompt-block.txt を手順書の最後に足す）。
囲みが無ければ、ログの末尾を本文にする:
  === 完了報告 ===
  要旨: <一番伝えたいこと1行>
  区分: 新規 / 改善・改良 / 変更 / 修正 / なし（当てはまるものをすべて）
  内容:
  - [新規] …
  === ここまで ===
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import smtplib
import ssl
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIGEST_DIR = BASE / "digest"
UNSENT_DIR = BASE / "unsent"
SENT_DIR = BASE / "sent"  # 送ったメールの控え。見張り役（watch/）が「報告が届いたか」をここで確かめる
SENT_KEEP_DAYS = 30
DEFAULT_CONF = Path.home() / ".config" / "ai-org-kit" / "mail.conf"
SMTP_TIMEOUT = 30
TZ_NAME = os.environ.get("AIORG_TZ_LABEL", "")  # 本文の時刻に添える名前（例: JST）。空なら付けない
WEEKDAYS = "月火水木金土日"
BLOCK_RE = re.compile(r"^[ \t]*=== 完了報告 ===\s*\n(.*?)\n[ \t]*=== ここまで ===", re.S | re.M)
LOG_TAIL_LINES = 80
BODY_MAX_CHARS = 40000
SUBJECT_SUMMARY_CHARS = 80
TIMEOUT_STATUS = 142  # perl の alarm で打ち切られたときの終了コード（README の「ジョブの呼び方の例」）
ENTRY_MARK = "<!-- entry -->"  # まとめの1件の区切り（本文に「### 」があっても数え違えない）


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def date_label(moment: datetime) -> str:
    return f"{moment:%m-%d}({WEEKDAYS[moment.weekday()]})"


def result_label(status: int) -> str:
    if status == 0:
        return "完了"
    if status == TIMEOUT_STATUS:
        return "打ち切り（時間の上限）"
    return f"失敗（終了コード {status}）"


def read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_block(text: str) -> dict[str, str]:
    """ログの最後の「完了報告」の囲みから 要旨・区分・内容 を取り出す。無ければ空"""
    matches = BLOCK_RE.findall(text)
    if not matches:
        return {}
    block = "\n".join(line.strip() for line in matches[-1].splitlines())
    fields: dict[str, str] = {}
    for key in ("要旨", "区分"):
        found = re.search(rf"^{key}[:：]\s*(.+)$", block, re.M)
        if found:
            fields[key] = found.group(1).strip()
    content = re.search(r"^内容[:：]\s*\n?(.*)$", block, re.M | re.S)
    if content:
        fields["内容"] = content.group(1).strip()
    return fields


def tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def compose(job: str, status: int, log: Path | None, category: str | None, summary: str | None,
            body: str | None) -> tuple[str, str]:
    log_text = read_text(log)
    block = parse_block(log_text)
    summary = summary or block.get("要旨") or ("報告なし（ログを確認）" if status == 0 else "要確認")
    category = category or block.get("区分") or "不明（報告の囲みなし）"
    content = body or block.get("内容") or ""
    moment = now_local()
    subject = f"【{job}】{date_label(moment)} {result_label(status)}｜{summary[:SUBJECT_SUMMARY_CHARS]}"
    parts = [
        f"■ 結果: {result_label(status)}",
        f"■ 区分: {category}",
        "■ 内容:",
        content or "（Claude の報告の囲みがありませんでした。下のログ末尾を見てください）",
    ]
    if log_text and body is None and block:
        parts += ["", "■ 報告の全文:", BLOCK_RE.sub("", log_text).strip()]
    elif log_text and not block:
        parts += ["", f"■ ログ末尾（{LOG_TAIL_LINES}行）:", tail(log_text)]
    parts += ["", f"■ 送信: {moment:%Y-%m-%d %H:%M} {TZ_NAME}".rstrip()]
    if log:
        parts.append(f"■ ログ: {log}")
    text = "\n".join(parts)
    if len(text) > BODY_MAX_CHARS:
        text = text[:BODY_MAX_CHARS] + "\n…（長いので省略。ログを見てください）"
    return subject, text


def load_settings() -> dict[str, str]:
    """設定ファイル → 環境変数の順に読み、環境変数を優先する"""
    settings: dict[str, str] = {}
    conf_path = Path(os.environ.get("AIORG_MAIL_CONF", str(DEFAULT_CONF))).expanduser()
    if conf_path.exists():
        mode = conf_path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"注意: {conf_path} がほかの人にも読める設定です。chmod 600 にしてください", file=sys.stderr)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(conf_path, encoding="utf-8")
        for section, key, name in (("mail", "to", "to"), ("mail", "from", "from"), ("smtp", "host", "host"),
                                   ("smtp", "port", "port"), ("smtp", "user", "user"),
                                   ("smtp", "password", "password")):
            value = parser.get(section, key, fallback="").strip()
            if value and not value.startswith("<"):  # 例のまま（<...>）の所は未設定として扱う
                settings[name] = value
    for name, env in (("to", "AIORG_MAIL_TO"), ("from", "AIORG_MAIL_FROM"), ("host", "AIORG_SMTP_HOST"),
                      ("port", "AIORG_SMTP_PORT"), ("user", "AIORG_SMTP_USER"),
                      ("password", "AIORG_SMTP_PASSWORD")):
        value = os.environ.get(env, "").strip()
        if value:
            settings[name] = value
    settings.setdefault("port", "465")
    settings.setdefault("from", settings.get("user", ""))
    if "password" in settings:
        settings["password"] = settings["password"].replace(" ", "")  # Gmail のアプリパスワードは空白入りで表示される
    return settings


def missing_settings(settings: dict[str, str]) -> list[str]:
    return [name for name in ("to", "host", "user", "password") if not settings.get(name)]


def notify_mac(title: str, message: str) -> None:
    """macOS の通知を出す（macOS 以外では何もしない）"""
    if sys.platform != "darwin":
        return
    script = (f'display notification "{message[:200].replace(chr(34), chr(39))}" '
              f'with title "{title.replace(chr(34), chr(39))}"')
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def launchd_label() -> str:
    """このプロセスを起動した launchd のジョブ名。親プロセスをたどって launchctl list の PID と照らし合わせる。
    見つからなければ XPC_SERVICE_NAME、それも無ければ空（手で動かした回など）"""
    env = os.environ.get("XPC_SERVICE_NAME", "")
    if sys.platform != "darwin":
        return ""
    try:
        listed = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        by_pid = {}
        for line in listed.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) == 3 and cols[0].isdigit():
                by_pid[int(cols[0])] = cols[2]
        pid = os.getpid()
        for _ in range(20):
            if pid in by_pid:
                return by_pid[pid]
            parent = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            if not parent.isdigit() or int(parent) <= 1:
                break
            pid = int(parent)
    except (OSError, subprocess.SubprocessError):
        pass
    return env if env not in ("", "0") else ""


def save_copy(subject: str, body: str, sent: bool) -> None:
    """メールの控えを sent/ に1件1ファイルで残す。控えが書けなくてもメールの送信は止めない"""
    try:
        SENT_DIR.mkdir(parents=True, exist_ok=True)
        moment = now_local()
        record = {"at": moment.isoformat(), "label": launchd_label(), "subject": subject, "body": body, "sent": sent}
        copy = SENT_DIR / f"{moment:%Y%m%d-%H%M%S}-{os.getpid()}.json"
        copy.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        copy.chmod(0o600)
        cutoff = (moment - timedelta(days=SENT_KEEP_DAYS)).timestamp()
        for old in SENT_DIR.glob("*.json"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError:
        pass


def deliver(subject: str, body: str, to: str | None) -> tuple[bool, str]:
    settings = load_settings()
    if to:
        settings["to"] = to
    missing = missing_settings(settings)
    if missing:
        return False, "設定が足りない: " + "・".join(missing)
    message = EmailMessage()
    message["From"] = settings["from"]
    message["To"] = settings["to"]
    message["Subject"] = subject
    message.set_content(body)
    port = int(settings["port"])
    error = ""
    for _ in range(2):  # ネットワークの一時的な失敗に1回だけ再試行
        try:
            if port == 465:
                with smtplib.SMTP_SSL(settings["host"], port, timeout=SMTP_TIMEOUT,
                                      context=ssl.create_default_context()) as smtp:
                    smtp.login(settings["user"], settings["password"])
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(settings["host"], port, timeout=SMTP_TIMEOUT) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(settings["user"], settings["password"])
                    smtp.send_message(message)
            return True, ""
        except (OSError, smtplib.SMTPException) as exc:
            error = str(exc)
    return False, error


def send_mail(subject: str, body: str, to: str | None, dry_run: bool) -> bool:
    """送れなければ unsent/ に残して通知を出す（呼び出し元の処理は止めない）"""
    if dry_run:
        print(f"[dry-run] 送らずに表示します\n件名: {subject}\n\n{body}")
        return True
    sent, error = deliver(subject, body, to)
    save_copy(subject, body, sent)
    if sent:
        return True
    UNSENT_DIR.mkdir(parents=True, exist_ok=True)
    unsent = UNSENT_DIR / f"{now_local():%Y%m%d-%H%M%S}-{os.getpid()}.txt"
    unsent.write_text(f"件名: {subject}\n送れなかった理由: {error}\n\n{body}", encoding="utf-8")
    unsent.chmod(0o600)
    print(f"完了報告メールを送れませんでした: {error}（{unsent} に残しました）", file=sys.stderr)
    notify_mac("完了報告メールを送れませんでした", f"{subject[:60]} → {unsent}")
    return False


def digest_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit("--digest は英数字・ハイフン・下線だけにしてください")
    return DIGEST_DIR / f"{name}.pending.md"


def cmd_send(args: argparse.Namespace) -> int:
    subject, body = compose(args.job, args.status, args.log, args.category, args.summary, args.body)
    return 0 if send_mail(subject, body, None, args.dry_run) else 1


def cmd_raw(args: argparse.Namespace) -> int:
    body = args.body if len(args.body) <= BODY_MAX_CHARS else args.body[:BODY_MAX_CHARS] + "\n…（長いので省略）"
    return 0 if send_mail(args.subject, body, args.to, args.dry_run) else 1


def cmd_digest_add(args: argparse.Namespace) -> int:
    if args.status != 0:
        return cmd_send(args)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    entry = f"{ENTRY_MARK}\n### {now_local():%m-%d %H:%M} {args.job}\n{(args.body or '').strip()}\n\n"
    with digest_path(args.digest).open("a", encoding="utf-8") as f:
        f.write(entry)
    return 0


def cmd_digest_send(args: argparse.Namespace) -> int:
    """まとめを送る。0件の日は送らない"""
    pending = digest_path(args.digest)
    if not pending.exists():
        return 0
    sending = pending.with_name(f"{args.digest}.sending-{now_local():%Y%m%d-%H%M%S}.md")
    pending.rename(sending)  # 送っている間に積まれた分は次のまとめに入る
    entries = read_text(sending)
    count = entries.count(ENTRY_MARK)
    body = entries.replace(f"{ENTRY_MARK}\n", "").strip()
    if not body:
        sending.unlink()
        return 0
    subject, text = compose(args.job, 0, None, args.category or "なし（記録のまとめ）", f"{count}件の実行", body)
    if not send_mail(subject, text, None, args.dry_run):
        sending.rename(pending.with_name(f"{args.digest}.failed-{now_local():%Y%m%d-%H%M%S}.md"))
        return 1
    if args.dry_run:
        sending.rename(pending)
        return 0
    sent_dir = DIGEST_DIR / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    sending.rename(sent_dir / f"{args.digest}-{now_local():%Y%m%d-%H%M}.md")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """設定がそろっているかだけを確かめる（パスワードは表示しない）"""
    settings = load_settings()
    for name in ("to", "from", "host", "port", "user"):
        print(f"{name}: {settings.get(name) or '（未設定）'}")
    print(f"password: {'設定あり' if settings.get('password') else '（未設定）'}")
    missing = missing_settings(settings)
    if missing:
        print("足りない設定: " + "・".join(missing))
        return 1
    print("設定はそろっています（実際に送れるかは --dry-run を外した send で確かめる）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自動実行の完了報告メール。send / digest-add / digest-send / raw / check のどれかを指定する",
    )
    parser.add_argument("--dry-run", action="store_true",
                        default=os.environ.get("AIORG_MAIL_DRY_RUN") == "1",
                        help="送らずに件名と本文を画面に出す")
    sub = parser.add_subparsers(dest="command", required=True)
    helps = {
        "send": "1回分の完了報告を送る",
        "digest-add": "1日のまとめに1件積む（失敗の回はすぐ送る）",
        "digest-send": "積んだまとめを1通で送る（0件なら送らない）",
    }
    for name, text in helps.items():
        p = sub.add_parser(name, help=text)
        p.add_argument("--job", required=True, help="件名の【】に入る名前")
        p.add_argument("--category", help="新規 / 改善・改良 / 変更 / 修正 / なし")
        if name != "digest-send":
            p.add_argument("--status", type=int, default=0, help="ジョブの終了コード（0=完了）")
            p.add_argument("--log", type=Path, help="ジョブのログ（完了報告の囲みをここから読む）")
            p.add_argument("--summary", help="件名に入る要旨（囲みより優先）")
            p.add_argument("--body", help="本文（囲みより優先）")
        if name != "send":
            p.add_argument("--digest", required=True, help="まとめの名前（英数字）")
    raw = sub.add_parser("raw", help="件名・本文をそのまま送る")
    raw.add_argument("--subject", required=True)
    raw.add_argument("--body", required=True)
    raw.add_argument("--to", help="宛先（省略時は設定の宛先）")
    sub.add_parser("check", help="設定がそろっているかを確かめる（送らない）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    handlers = {"send": cmd_send, "digest-add": cmd_digest_add, "digest-send": cmd_digest_send,
                "raw": cmd_raw, "check": cmd_check}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
