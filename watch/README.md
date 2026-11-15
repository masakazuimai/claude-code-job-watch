# 見張り役（watch）

毎晩1回、自分の自動実行を全部見て、問題があれば完了報告メールで知らせる。OS で見方が違う。

- **macOS（launchd）**: `~/Library/LaunchAgents` の plist のうち、`label_prefixes` で始まる名前のものを読む
- **Linux・WSL（cron）**: `config.json` の `cron_jobs`（名前と予定）と、完了報告の控え（`../sent/`）だけで判定する

| 出る言葉 | 意味 | よくある原因 | 出る OS |
|---|---|---|---|
| 止まっている | plist はあるのに launchd に読み込まれていない | `launchctl bootstrap` を忘れた・再起動後に読み込まれていない | macOS |
| 常駐が落ちている | KeepAlive のジョブが動いていない | 起動直後に落ちている（ログを見る） | macOS |
| 報告が届いていない | 予定の時刻から `grace_hours` たっても完了報告が来ていない | スクリプトの途中で止まった・メールの設定が切れた・crontab の行と `cron_jobs` の時刻や名前が違う | 両方 |
| 機械が止まっていた | 予定の時刻に機械が眠っていた・電源が切れていた | 異常には数えない。macOS はその時刻のシステムのログの有無、Linux・WSL は起動した時刻（`/proc/stat`・`uptime -s`・`who -b`）が予定より後かで見る。Linux のスリープは見分けられず「報告が届いていない」になる | 両方 |
| 停止の登録と食い違い | `paused` に書いたジョブが読み込まれている | 再開したのに `paused` から外し忘れた | macOS |

## 使い方

1. `cp config.example.json config.json` で設定を作る
   - macOS: `label_prefixes` と `self_label` を自分のジョブ名の接頭辞に合わせる
   - Linux・WSL: `cron_jobs` に、crontab に入れたジョブを1件ずつ書く。`name` は run.sh の `JOB_NAME`（メールの件名の【】）、`schedule` は crontab の時刻の5つの欄と同じにする（`*`・数・`1-5`・`*/15`・`1,15` が使える。`@daily` は使えない）
2. 手で1回動かす: `bash run.sh`（送らずに確かめたいときは、先に `export AIORG_MAIL_DRY_RUN=1`）
3. 毎晩動かす: macOS は `../templates/watch.plist`、Linux・WSL は `../templates/crontab.example` の行（`__HOME__` や `<ユーザー名>` を自分の場所に直す。手順は `../README.md`）

## 決まり

- 意図して止めるジョブは、止める作業の中で `paused` に理由つきで書く（macOS は Label、Linux・WSL は `cron_jobs` の `name`）。再開したら同じ作業の中で外す
- 見張り役は機械の時間帯で予定を数える。cron だけ時間帯を変える（`CRON_TZ=`）と数え方がずれる
- 完了報告の控え（`../sent/`）は `report_mail.py` が書く。見張り役はそれを読むだけで、メールボックスは見ない
- 作られるファイル（`status.json`・`last_seen.json`・`job_names.json`・`loaded_since.json`・`logs/`）は手で直さない
