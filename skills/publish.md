# publish: メタ生成・多言語化・YouTube アップロードドメイン

## 目的
タイトル・説明・タグを生成し、多言語 metadata を作成し、YouTube Data API でアップロードまたは snippet 更新する。

## 入口コマンド
- メタ: `python3 Python/studio.py meta --vol <N> --dry-run`
- 多言語: `python3 Python/studio.py localization --vol <N> --dry-run`
- アップロード: `python3 Python/studio.py upload --vol <N> --dry-run`

## 前提リソース
- Claude/Codex CLI（`claude_proposer.py` と `app_llm_runner.py`）
- channel folder の `.youtube_token.json`
- `youtube_client_secret.json`
- 書き出し済み MP4

## 並列可否
- meta / localization は LLM quota に注意して小並列可。
- upload は per-channel 単位で順次。`youtube_upload.json` marker と既存動画チェックを尊重する。

## 典型手順
1. `meta` で `youtube_title.txt` / `youtube_description.txt` / `youtube_tags.txt` を作る。
2. `localization` は `youtube_upload_defaults.localization_languages` が空なら no-op。
3. `upload` は予約日時が解決できると private + publishAt で送る。

## 失敗時の対処
- OAuth エラー: `python3 Python/app_youtube.py --auth-only --channel-folder <channel_folder>`。
- quota exhausted: exit 77。短期 retry しない。
- 再アップロードが必要: `APP_FORCE_REUPLOAD=1` を明示。
