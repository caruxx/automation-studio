# App Routing

Automation Studio の正規入口は `_claude` ルートから実行する `python3 Python/studio.py`。
直接 `app_pipeline.py` や curl を叩く前に、studio.py で intent 解決・vol 解決・チャンネルガードを通す。

## 基本操作

```bash
python3 Python/studio.py --list
python3 Python/studio.py --explain meta
python3 Python/studio.py resolve 78
python3 Python/studio.py bgimage --vol 78 --dry-run
python3 Python/studio.py bgimage --vol 78
python3 Python/studio.py pipeline --vol 78 --from premiere
```

`--dry-run` は解決後の CLI コマンドまたは curl 相当を表示するだけで、副作用はない。
標準出力の末尾には AI が読むための 1 行 JSON が出る。

## Web サーバー再起動

launchd 常駐時はコードがローカルミラーから起動するため、コード変更の反映と再起動をまとめて行う。

```bash
bash setup_launchd.sh --sync
launchctl list | grep automation
```

非常駐時だけ直接起動する。

```bash
bash Python/start.sh
```

## チャンネルガード

実行前に active channel を表示する。
`--channel <id>` が active channel と違う場合は exit 3 で停止する。
明示的に切り替える場合だけ `--switch` を付ける。

```bash
python3 Python/studio.py resolve 78 --channel sukima
python3 Python/studio.py bgimage --vol 78 --channel sukima --switch --dry-run
```

解決した `video_name` が active channel のフォルダ配下でなければ実行しない。
チャンネル混線を避けるため、vol 番号だけで判断しない。

## via-api ルール

`Python/routes.json` の `via_api_safe=false` は `--via-api` 禁止。
特に `meta` / `localization` は LLM 長時間 step なので、Web API の 10 秒 timeout を踏まないよう CLI 直実行にする。

```bash
python3 Python/studio.py meta --vol 78
python3 Python/studio.py meta --vol 78 --via-api  # 拒否される
```

## Exit Code

- `0`: 成功
- `1`: 委譲先 CLI/API の一般失敗
- `2`: intent / vol 解決 / via-api ガードの失敗
- `3`: チャンネルガードの失敗
- `75`: unattended_login
- `76`: retryable
- `77`: quota_exhausted
- `78`: preflight_fail

既存 CLI に委譲した場合、`75` から `78` の sentinel exit code はそのまま透過する。

## 並行運用ルール

複数エージェントで同時作業する場合は、書き込み対象を必ず分ける。

- 1 エージェント = 1 チャンネル、または 1 ドメイン（music/image/video/qa/publish）
- 同じチャンネルの同じ vol に対して、複数エージェントが同時にファイル生成・投稿・設定保存をしない
- Premiere / export は render queue が物理直列化するが、手動作業側でも同時操作を避ける
- vol 番号だけで判断せず、`studio.py resolve <vol> --channel <id>` か `/api/resolve-vol/{vol}?channel_id=<id>` で folder を確認する

## Autopilot

autopilot はチャンネル単位で既定 OFF。ON にしたチャンネルだけ、APScheduler の worker tick が dry-run 判定済み候補を投入する。

```bash
# 状態と dry-run 候補を確認
curl -s http://localhost:8888/api/workers/status

# チャンネル別に ON/OFF
curl -s -X POST http://localhost:8888/api/workers/autopilot \
  -H 'Content-Type: application/json' \
  -d '{"channel_id":"<id>","enabled":true}'

curl -s -X POST http://localhost:8888/api/workers/autopilot \
  -H 'Content-Type: application/json' \
  -d '{"channel_id":"<id>","enabled":false}'
```

autopilot 経由の upload は QA 成功後だけ実行される。公開設定は `private` + フォルダ名の publish date 由来の `publishAt` が既定で、チャンネル別 OAuth トークン（`<channel_folder>/.youtube_token.json`）が無い場合は停止する。過去日の publish date は投稿せず通知対象にする。

更新後に `autopilot_suspended_by_update` が立った場合は、UI の再開ボタンまたは次で全解除する。

```bash
python3 Python/studio.py autopilot --resume-all
```

## ユーザーの好み指示

文体・音楽スタイル・ベンチマーク先・投稿設定など、今後も使うユーザーの好み指示は一回限りのチャット対応で終わらせず、`settings_catalog.json` を検索して `studio.py config set` で永続化する。

```bash
python3 Python/studio.py config search 投稿
python3 Python/studio.py config set channel.suno.prompt "quiet late-night jazz house..." --channel <id>
python3 Python/studio.py config get channel.suno.prompt --channel <id>
```
