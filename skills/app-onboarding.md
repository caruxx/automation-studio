# app-onboarding: 新チャンネル立ち上げ

新チャンネルはチェックリスト駆動で進める。状態は各チャンネルフォルダ直下の
`onboarding.json` が真実源。

## CLI

```bash
cd <_claudeのルート>/Python
python3 studio.py channel-onboard --channel <channel_id> --status
python3 studio.py channel-onboard --channel <channel_id>
python3 studio.py channel-onboard --channel <channel_id> --step benchmark_set --urls "https://www.youtube.com/@example"
```

`oauth` と `benchmark_set` は人間の入力が必要な場合、exit 0 で停止し、
末尾 JSON の `next_action` に次の操作を出す。

## Web UI

基本設定 → 運営チャンネル管理 → `新規チャンネル(かんたん作成)`。

ウィザードは以下の API を使う。

- `POST /api/channels`
- `GET /api/channels/{id}/onboarding`
- `POST /api/channels/{id}/onboarding/{step}`

詳細設定画面は残し、ウィザードは新規立ち上げの漏れ防止だけを担当する。

## ステップ

1. `register`: registry、フォルダ、`.app_channel_config.json`、`onboarding.json`
2. `oauth`: `<channel_folder>/.youtube_token.json`
3. `benchmark_set`: `.app_channel_config.json` の `rival_channels`
4. `benchmark_fetch`: 既存 `app_competitor.py` による競合取得
5. `analyze`: `app_benchmark_concept.py`、`app_benchmark_thumbnail.py`、`app_benchmark_title.py`
6. `concept_apply`: 分析結果を空欄のチャンネル設定へ落とし込み
7. `first_vol`: vol.1 フォルダと `plan.json`
8. `verify_loop`: APScheduler の日次 `onboarding_verify_loop`

## 注意

- 既存チャンネルは初回 `GET /api/channels/{id}/onboarding` 時に現状から推定して
  `onboarding.json` だけを新規作成する。
- 初回推定では既存 `.app_channel_config.json` を変更しない。
- 分析はバックグラウンドタスクで実行する。Web API の同期レスポンスで LLM を待たない。
- `verify_loop` は YouTube Data API の読み取りのみ。1日1回、Discord に簡易比較レポートを送る。
