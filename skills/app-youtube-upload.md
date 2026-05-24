# app-youtube-upload: YouTube アップロード自動化

YouTube Data API v3 で動画をアップロードし、**動画フォルダ内の保存ファイル**から
タイトル・説明・タグ・サムネイルを自動で組み立てるスキル。

## 入力（動画フォルダ内から自動読込）

| ファイル | 役割 | 未存在時のフォールバック |
|---------|------|----------------------|
| `vol_vol*.mp4` | 本体 | チャンネルフォルダの外部 export_path も検索 |
| `vol{N}.jpg` / `サムネイル.jpg` / `vol{N}.png` | サムネイル | 無し（サムネ設定スキップ） |
| `youtube_title.txt` | タイトル | `vol.{N}` |
| `youtube_description.txt` | 説明文 | 空 |
| `youtube_tags.txt`（改行 or カンマ区切り） | タグ配列 | 既定タグ（`BGM, Lounge, Chill, Relax, Study, Work, AI Music, SUNO`） |

**保存ファイルの作成は** Web ダッシュボード（[app-web-dashboard.md](./app-web-dashboard.md)）
の「詳細」タブから行う。`✨ AI提案` ボタンで Claude CLI が作成（[app-ai-propose.md](./app-ai-propose.md) 参照）。

## 出力

| ファイル | 役割 |
|---------|------|
| `youtube_upload.json` | 完了マーカー。`{video_id, url, title, privacy, schedule, uploaded_at}` — ダッシュボードのステッパーで「アップロード済」判定に使用 |

## 実行方法

### Web ダッシュボード（推奨）

**動画詳細 → アップロードタブ** から:
1. サムネイル画像プレビュー確認
2. タイトル（編集可）・説明文（読み取り専用プレビュー）・タグチップ確認
3. 公開設定（非公開/限定公開/公開）＋公開予約日時
4. 「▶ アップロード実行」→ 確認モーダル → YouTube ログ画面へ自動遷移

公開予約は `privacyStatus=private + publishAt=<ISO>` で送信（YouTube 側の仕様）。

### コマンドライン

```bash
# 保存ファイルを全部使って限定公開
python3 app_youtube.py /path/to/67_vol_260405 --privacy unlisted

# タイトル・タグを引数で上書き（カンマ区切り）
python3 app_youtube.py /path/to/67_vol_260405 \
  --title "Elegant Lounge Music | vol.67" \
  --tags "BGM,Lounge,Chill,Night Drive,AI Music" \
  --schedule "2026-04-15T09:00:00Z" --privacy private

# 初回認証のみ
python3 app_youtube.py --auth-only
```

### Web API

```
POST /api/youtube/upload
{
  "video_name": "67_vol_260405",   // or "folder": "/abs/path"
  "title": "...",                   // 省略時は youtube_title.txt
  "tags": ["..."],                  // 省略時は youtube_tags.txt
  "privacy": "unlisted",
  "schedule": "2026-04-15T00:00:00.000Z"
}
```

## セットアップ

### 1. OAuth クライアントシークレット
`~/.config/{app_id}/youtube_client_secret.json` を Google Cloud Console から取得・配置

### 2. 初回認証
```bash
python3 app_youtube.py --auth-only
```
ブラウザで認証 → `~/.config/{app_id}/youtube_token.json` に保存

### 3. 依存パッケージ
```bash
pip3 install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```
または `setup.sh` で一括導入。

## 関数

実体は [Python/app_youtube.py](../Python/app_youtube.py)。主要関数:

| 関数 | 役割 |
|------|------|
| `get_credentials()` | OAuth 認証・リフレッシュ |
| `find_video_file(folder)` | MP4 検索（`vol_vol*.mp4` 優先） |
| `find_thumbnail(folder)` | サムネ候補検索 |
| `load_title(folder)` | `youtube_title.txt` 読込（未存在で空文字） |
| `load_description(folder)` | `youtube_description.txt` 読込 |
| `load_tags(folder)` | `youtube_tags.txt` → 配列、無ければ既定タグ |
| `upload_video(folder, title, schedule, privacy, tags)` | メイン処理、完了後マーカー書出 |

## API リクエスト構造

```python
body = {
  "snippet": {
    "title": title,                # youtube_title.txt or 引数
    "description": description,    # youtube_description.txt
    "tags": tags,                  # youtube_tags.txt or 引数 or 既定
    "categoryId": "10",            # Music 固定
    "defaultLanguage": "en",
    "defaultAudioLanguage": "en",
  },
  "status": {
    "privacyStatus": privacy,
    "selfDeclaredMadeForKids": False,
  },
}
# 予約公開時
if schedule:
    body["status"]["privacyStatus"] = "private"
    body["status"]["publishAt"] = schedule
```

チャンクサイズ 10MB、進捗ログは毎チャンクで `% / MB/s / 残り` を表示。
サムネは `youtube.thumbnails().set()` でアップロード後に設定。

## トラブルシューティング

| 症状 | 原因 | 対策 |
|------|------|------|
| `CLIENT_SECRET が見つかりません` | 初期設定未完了 | `~/.config/{app_id}/youtube_client_secret.json` を配置 |
| ブラウザ認証画面が出ない | `--auth-only` でない実行 | 先に `--auth-only` で認証を済ませる |
| タグが既定のまま | `youtube_tags.txt` 未保存 | 詳細タブでタグ欄に入力 → 保存 |
| サムネイル未設定 | `vol{N}.jpg` が無い | 「画像」タブで main にして `vol{N}.jpg` にリネーム、または `サムネイル.jpg` を配置 |
| 重複アップロード | マーカー無視で再実行 | アップロードタブの「再アップロード」ボタンで明示的に実行 |
