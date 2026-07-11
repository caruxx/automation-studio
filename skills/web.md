# web: FastAPI / Web UI / 設定ドメイン

## 目的
Automation Studio の Web UI、FastAPI ルート、設定保存、認証、タスク状態を扱う。

## 入口コマンド
- 起動: `python3 Python/studio.py web-start --dry-run`
- 直接: `bash Python/start.sh`
- ヘルス: `curl -s http://localhost:8888/api/health`

## 前提リソース
- FastAPI / uvicorn
- `Python/app.py`
- `web/static/index.html` と `web/static/js/*`
- shared config と per-channel `.app_channel_config.json`

## 並列可否
- Web サーバー起動・再起動は per-machine 1。
- API の内部タスクは `active_tasks` / `_ensure_not_running()` で一部排他される。

## 典型手順
1. app.py 変更後は reload 無しのため `bash Python/start.sh` で再起動。
2. ルート追加・変更は `/openapi.json` または対象 API を実際に叩いて確認。
3. 設定は global / channel / machine-dependent の保存先を混ぜない。

## 失敗時の対処
- 8888 応答なし: `launchctl list | grep automation` と `bash setup_launchd.sh --sync`。
- ルート未反映: サーバー再起動漏れを疑う。
- channel 設定混線: `APP_CHANNEL_FOLDER` と active channel を確認。
