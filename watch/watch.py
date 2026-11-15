"""自動実行の見張り役。各ジョブが予定どおり動いて完了報告を送ったかを毎晩確かめる。

OS で見方を分ける。

macOS（launchd）: ~/Library/LaunchAgents の plist を読んで判定する
- 止まっている: plist があるのに launchd に読み込まれていない（config の paused は除く）
- 常駐が落ちている: KeepAlive のジョブが読み込まれているのに動いていない
- 報告が届いていない: 最後の予定時刻から grace_hours たっても、その時刻以降の完了報告が sent/ に無い
- 機械が止まっていた: 報告が届いていない回のうち、予定時刻の前後にシステムのログが1行も無い（電源オフ・スリープ）回。異常には数えない
- 停止の登録と食い違い: config の paused に書いたのに読み込まれている

Linux・WSL（cron）: config の cron_jobs（予定の表）と完了報告の控え（sent/）だけで判定する
- 報告が届いていない: 上と同じ
- 機械が止まっていた: 起動した時刻（/proc/stat・uptime -s・who -b）が予定時刻より後の回。異常には数えない
  （スリープは見分けられない。起動時刻が取れなければ「報告が届いていない」のまま）
- 「止まっている」「常駐が落ちている」は cron には無いので出さない

結果は status.json に書き、最後の2行に件数と「要旨: …」を出す（run.sh がメールの要旨に使う）。
完了報告の控え（../sent/）は report_mail.py が書く。見張り役はそれを読むだけ。
"""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
CONFIG_PATH = HERE / "config.json"
LAST_SEEN = HERE / "last_seen.json"  # sent/ は30日で消えるので、最後に届いた時刻をここに残す
JOB_NAMES = HERE / "job_names.json"  # 件名の【】の名前 → label（macOS で手で動かした回の控えは label が空になるため）
LOADED_SINCE = HERE / "loaded_since.json"  # macOS: 止まっていたジョブが読み込まれた（再開された）時刻
NAME = re.compile(r"^【(.+?)】")
IS_MAC = sys.platform == "darwin"
STATUS_NAMES = {
    "unloaded": "止まっている",
    "resident_down": "常駐が落ちている",
    "missing": "報告が届いていない",
    "paused_loaded": "停止の登録と食い違い",
}
LINUX_STATUS_NAMES = {"missing": "報告が届いていない"}
OFF_NAME = "機械が止まっていた"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"設定がありません: {CONFIG_PATH}（config.example.json を写して作る）")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config.setdefault("label_prefixes", [])
    config.setdefault("grace_hours", 3)
    config.setdefault("off_window_minutes", 2)
    config.setdefault("lookback_days", 40)
    config.setdefault("paused", {})
    config.setdefault("event_only", {"prefixes": []})
    config.setdefault("cron_jobs", [])
    return config


CONFIG = load_config()


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def sent_records() -> list[dict]:
    records = []
    for f in (BASE / "sent").glob("*.json"):
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("sent") and record.get("at"):  # 送れずに unsent/ に残った分は届いた扱いにしない
            records.append(record)
    return records


def job_name(record: dict) -> str | None:
    match = NAME.match(record.get("subject", ""))
    return match.group(1) if match else None


# ---------- 予定の計算（共通） ----------

def last_scheduled(calendar, now: datetime) -> datetime | None:
    """launchd の StartCalendarInterval（dict か list）から、now 以前の最後の予定時刻を返す"""
    entries = calendar if isinstance(calendar, list) else [calendar]
    for back in range(CONFIG["lookback_days"] + 1):
        day = (now - timedelta(days=back)).replace(second=0, microsecond=0)
        latest = None
        for entry in entries:
            if "Month" in entry and entry["Month"] != day.month:
                continue
            if "Day" in entry and entry["Day"] != day.day:
                continue
            if "Weekday" in entry and entry["Weekday"] % 7 != (day.weekday() + 1) % 7:  # 日曜=0（7も日曜）
                continue
            hours = [entry["Hour"]] if "Hour" in entry else range(24)
            for hour in hours:
                at = day.replace(hour=hour, minute=entry.get("Minute", 0))
                if at <= now and (latest is None or at > latest):
                    latest = at
        if latest:
            return latest
    return None


def cron_field(text: str, low: int, high: int) -> set[int]:
    """cron の1つの欄（* ・ 数 ・ 範囲 a-b ・ 間隔 */n a-b/n ・ カンマ区切り）を数の集まりにする"""
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
        else:
            start = end = int(part)
        if start < low or end > high or step < 1:
            raise ValueError(f"範囲の外: {text}")
        values.update(range(start, end + 1, step))
    return values


def last_cron(schedule: str, now: datetime) -> datetime | None:
    """cron の予定（分 時 日 月 曜日）から、now 以前の最後の予定時刻を返す"""
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("cron の予定は「分 時 日 月 曜日」の5つの欄で書く（@daily などは使えない）")
    minutes = sorted(cron_field(fields[0], 0, 59), reverse=True)
    hours = sorted(cron_field(fields[1], 0, 23), reverse=True)
    days = cron_field(fields[2], 1, 31)
    months = cron_field(fields[3], 1, 12)
    weekdays = {d % 7 for d in cron_field(fields[4], 0, 7)}
    day_any, weekday_any = fields[2] == "*", fields[4] == "*"
    for back in range(CONFIG["lookback_days"] + 1):
        day = (now - timedelta(days=back)).replace(second=0, microsecond=0)
        if day.month not in months:
            continue
        dom_ok = day.day in days
        dow_ok = (day.weekday() + 1) % 7 in weekdays
        if day_any or weekday_any:
            matched = dom_ok and dow_ok
        else:
            matched = dom_ok or dow_ok  # cron は日と曜日の両方を決めると「どちらか」で動く
        if not matched:
            continue
        for hour in hours:
            for minute in minutes:
                at = day.replace(hour=hour, minute=minute)
                if at <= now:
                    return at
    return None


def judge(row: dict, due: datetime | None, last: str | None, now: datetime, written: datetime,
          machine_off) -> dict:
    """予定と最後の報告から ok / mac_off / missing を決める（macOS・Linux 共通）"""
    row = {**row, "lastScheduled": due.isoformat() if due else None, "lastReport": last}
    if due is None or now - due < timedelta(hours=CONFIG["grace_hours"]):
        return {**row, "status": "ok", "note": "直近の予定はまだ確認待ち"}
    if due < written:  # 登録した・直した後の予定だけを数える
        return {**row, "status": "ok", "note": "登録後の最初の予定がまだ来ていない"}
    if last and datetime.fromisoformat(last) >= due - timedelta(minutes=5):
        return {**row, "status": "ok", "note": ""}
    if machine_off(due):
        return {**row, "status": "mac_off",
                "note": f"{due:%m-%d %H:%M} の回は機械が止まっていたため動いていない"}
    return {**row, "status": "missing", "note": f"{due:%m-%d %H:%M} の回の完了報告が届いていない"}


# ---------- macOS（launchd） ----------

def mac_last_seen(labels: set[str]) -> dict:
    records = sent_records()
    names = read_json(JOB_NAMES)
    for r in records:
        name = job_name(r)
        if name and r.get("label") in labels:
            names = {**names, name: r["label"]}
    seen = read_json(LAST_SEEN)
    for r in records:
        name = job_name(r)
        label = r["label"] if r.get("label") in labels else names.get(name) if name else None
        if label and r["at"] > seen.get(label, ""):
            seen = {**seen, label: r["at"]}
    write_json(JOB_NAMES, names)
    write_json(LAST_SEEN, seen)
    return seen


def update_loaded_since(states: dict, now: datetime) -> dict:
    """前回の見回りで読み込まれていなかったジョブが今回は読み込まれていたら、その時刻を再開の時刻として残す"""
    saved = read_json(LOADED_SINCE)
    result = {}
    for label, state in states.items():
        before = saved.get(label)
        if state is None:
            result[label] = "unloaded"
        elif before == "unloaded":
            result[label] = now.isoformat()
        else:
            result[label] = before
    write_json(LOADED_SINCE, result)
    return result


def launchd_state(label: str, user_id: str) -> dict | None:
    out = subprocess.run(["launchctl", "print", f"gui/{user_id}/{label}"], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return {"running": "state = running" in out.stdout}


def mac_was_off(at: datetime) -> bool:
    """予定時刻の前後 off_window_minutes 分にシステムのログが1行も無ければ、Macが止まっていたとみなす"""
    window = timedelta(minutes=CONFIG["off_window_minutes"])
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        out = subprocess.run(
            ["/usr/bin/log", "show", "--style", "compact",
             "--start", (at - window).strftime(fmt), "--end", (at + window).strftime(fmt)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False  # 調べられなかったときは「報告が届いていない」のまま出す
    if out.returncode != 0:
        return False
    return not any(line[:2] == "20" for line in out.stdout.splitlines())


def mac_check(label: str, plist: dict, seen: dict, now: datetime, written: datetime, state: dict | None) -> dict:
    row = {"label": label}
    if label in CONFIG["paused"]:
        if state is not None:
            return {**row, "status": "paused_loaded",
                    "note": "停止中の登録なのに読み込まれている（再開したなら config.json の paused から外す）"}
        return {**row, "status": "paused", "note": CONFIG["paused"][label]}
    if state is None:
        return {**row, "status": "unloaded", "note": "plist はあるが launchd に読み込まれていない"}
    if plist.get("KeepAlive"):
        ok = state["running"]
        return {**row, "status": "ok" if ok else "resident_down", "note": "常駐" if ok else "常駐なのに動いていない"}
    if any(label.startswith(p) for p in CONFIG["event_only"].get("prefixes", [])):
        return {**row, "status": "ok", "note": "出来事のある回だけ報告するジョブ（読み込みだけ確認）"}
    calendar = plist.get("StartCalendarInterval")
    if not calendar:
        return {**row, "status": "ok", "note": "予定時刻なし（確認対象外）"}
    return judge(row, last_scheduled(calendar, now), seen.get(label), now, written, mac_was_off)


def mac_rows(now: datetime) -> list[dict]:
    jobs = {}
    for path in sorted(AGENTS.glob("*.plist")):
        try:
            plist = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        label = plist.get("Label", path.stem)
        if label == CONFIG.get("self_label") or not any(label.startswith(p) for p in CONFIG["label_prefixes"]):
            continue
        jobs[label] = (path, plist)
    seen = mac_last_seen(set(jobs))
    user_id = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    states = {label: launchd_state(label, user_id) for label in jobs}
    loaded_since = update_loaded_since(states, now)
    rows = []
    for label, (path, plist) in jobs.items():
        # 数え始め＝plist を作った・直した時刻と、止まっていたのが再開された時刻の遅いほう
        written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone()
        resumed = loaded_since.get(label)
        if resumed and resumed != "unloaded":
            written = max(written, datetime.fromisoformat(resumed))
        rows.append(mac_check(label, plist, seen, now, written, states[label]))
    return rows


# ---------- Linux・WSL（cron） ----------

def boot_time() -> datetime | None:
    """機械（WSL なら WSL の中）が起動した時刻。/proc/stat → uptime -s → who -b の順に試す"""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), timezone.utc).astimezone()
    except (OSError, ValueError):
        pass
    for command in (["uptime", "-s"], ["who", "-b"]):
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})(:\d{2})?", out)
        if found:
            text = found.group(1) + (found.group(2) or ":00")
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()
    return None


def linux_last_seen(names: set[str]) -> dict:
    seen = read_json(LAST_SEEN)
    for r in sent_records():
        name = job_name(r)
        if name in names and r["at"] > seen.get(name, ""):
            seen = {**seen, name: r["at"]}
    write_json(LAST_SEEN, seen)
    return seen


def linux_rows(now: datetime) -> list[dict]:
    jobs = CONFIG["cron_jobs"]
    seen = linux_last_seen({j["name"] for j in jobs if j.get("name")})
    booted = boot_time()
    config_written = datetime.fromtimestamp(CONFIG_PATH.stat().st_mtime, timezone.utc).astimezone()
    rows = []
    for job in jobs:
        name = job.get("name", "")
        row = {"label": name, "schedule": job.get("schedule")}
        if name in CONFIG["paused"]:
            rows.append({**row, "status": "paused", "note": CONFIG["paused"][name]})
            continue
        try:
            due = last_cron(job["schedule"], now)
        except (KeyError, ValueError) as exc:
            rows.append({**row, "status": "missing", "note": f"予定の書き方が読めない: {exc}"})
            continue
        written = config_written  # cron_jobs に足した・直した後の予定だけを数える
        if job.get("added"):
            written = max(written, datetime.fromisoformat(job["added"]).astimezone())
        rows.append(judge(row, due, seen.get(name), now, written,
                          lambda at: booted is not None and booted > at))
    return rows


# ---------- まとめ ----------

def main() -> None:
    now = now_local()
    rows = mac_rows(now) if IS_MAC else linux_rows(now)
    names = STATUS_NAMES if IS_MAC else LINUX_STATUS_NAMES
    write_json(HERE / "status.json", {"checkedAt": now.isoformat(), "os": sys.platform, "jobs": rows})
    for r in (r for r in rows if r["status"] in names):
        print(f"- [{names[r['status']]}] {r['label']}: {r['note']}")
    for r in (r for r in rows if r["status"] == "mac_off"):
        print(f"- [{OFF_NAME}] {r['label']}: {r['note']}")
    for r in (r for r in rows if r["status"] == "paused"):
        print(f"- [停止中・意図どおり] {r['label']}: {r['note']}")
    counts = {k: sum(r["status"] == k for r in rows) for k in names}
    off = sum(r["status"] == "mac_off" for r in rows)
    print(f"確認 {len(rows)}件｜" + "・".join(f"{names[k]} {counts[k]}件" for k in names) + f"・{OFF_NAME} {off}件")
    problems = [f"{names[k]} {counts[k]}件" for k in names if counts[k]]
    if problems:
        print("要旨: 要確認：" + "・".join(problems))
    elif off:
        print(f"要旨: 問題なし（確認 {len(rows)}件・機械が止まっていて動かなかった回あり）")
    else:
        print(f"要旨: 問題なし（確認 {len(rows)}件）")


if __name__ == "__main__":
    main()
