# app-suno-download: SUNO Workspace 楽曲の一括ダウンロード

Playwright の永続プロファイルで SUNO にログイン済みのまま、
指定 Workspace の全トラックを MP3 で動画フォルダに保存する仕組み。

## 設計原則

SUNO の `/api/feed/?ids=UUID` を直接叩くと **認証（Clerk JWT）** が通らず 4xx を返す。
そのため **SUNO SPA 自身が送信する fetch/XHR レスポンスをインターセプト** して
`audio_url` を収集する。これは Tampermonkey ユーザースクリプト
「Suno 選択 & 一括ダウンロード」と同じ方式。

## 実装コンポーネント

### A. fetch/XHR インターセプタ (`_SUNO_AUDIO_URL_INTERCEPTOR`)

[Python/suno_auto_create.py](../Python/suno_auto_create.py) に定数として JS を保持し、
`context.add_init_script()` で **document-start 時点** にブラウザへ注入:

```
window.fetch          をラップ: /api/(feed|clips|v2/feed) のレスポンスをパース
XMLHttpRequest.open/send をラップ: 同上
→ window.__sunoAudioUrlCache[id] = { audioUrl, title }
```

インストール済み判定は `window.__sunoAudioInterceptorInstalled`。

### B. ステータスオーバーレイ (`_STATUS_OVERLAY_SCRIPT`)

自動操作中のブラウザ右下に黒いパネルで現在処理を表示。
`window.__appStatus(msg, variant)` を Python 側から `page.evaluate` 経由で呼ぶ。
variant: `info`/`ok`/`warn`/`err`（border 色で区別）。

### C. `download_workspace_tracks(page, workspace_name, target_dir)`

```
1. /me/workspaces へ遷移 → get_by_text(workspace_name, exact=True).click()
   → /create?wid=XXX にリダイレクト
2. _collect_all_song_uuids(page):
   - [data-testid="clip-row"] の a[href*="/song/"] から UUID 抽出
   - スクロール可能親要素を探して lazy-load 分まで全部スクロール
   - 安定するまで（3回連続追加なし）最大40回
3. 2秒待機で __sunoAudioUrlCache が埋まるのを確認
4. キャッシュ件数が UUID 数の半分未満なら from_top=True で再スクロール
5. 各 UUID について cache から audio_url 引く
   （cache miss は最大5件まで直接 fetch でリトライ）
6. page.context.request.get(audio_url, timeout=60000) で MP3 取得
   - Cookie 共有済の APIRequestContext
   - 1KB 未満 / HTTP エラーはスキップ
7. target_dir に <title>.mp3 で保存（同名衝突は _2, _3 でリネーム）
```

## API / UI

### Web ダッシュボード
動画詳細「楽曲」タブの **「⬇ Workspace DL」** ボタン

### API
```
POST /api/suno/download
{
  "workspace": "vol_vol77",        // 直接指定
  "video_name": "77_vol_260416",   // or vol.XX 名から {channel}_vol{N} を自動計算
  "target_dir": "/abs/path"         // 省略時は動画フォルダ直下に保存
}
```

### CLI
```bash
python3 suno_auto_create.py \
  --download-workspace vol_vol77 \
  --download-dir /abs/path/to/video_folder
```

## 保存先

- `video_name` 指定時: **動画フォルダ直下**（`original_music/` には入れない）
- `target_dir` 明示時: そのパス
- どちらも無い: `~/Downloads/suno_{workspace}/`

フォルダ直下に展開するのは、その後の [app-rename-audio](./app-rename-audio.md)
（リネーム + ffmpeg）が直下の `*.mp3` を対象とするため。

## よくあるトラブル

| 症状 | 原因 | 対処 |
|------|------|------|
| `audio_url 取得失敗 (cache miss)` が全件 | インターセプタ未注入（既存タブを使い回し等） | 該当 Playwright コンテキストを閉じて再起動 |
| UUID 検出数が少ない | lazy-load 未到達 | `_collect_all_song_uuids(from_top=True)` で再収集 |
| HTTP 403 / 空データ | `audio_url` の署名期限切れ | Workspace を開き直して再取得（インターセプタが最新 URL をキャッシュ） |
| 「Download ボタンが…」 | 古い実装 | 現行は UI クリック不要。API ベース方式に刷新済み |

## 参考: userscript 原典

元実装 [_Tampermonkey/suno-selection-bulk-downloader.user.js](../../DEV/_Tampermonkey/suno-selection-bulk-downloader.user.js)

- Alt+Click で選択、スクロール全選択、`showDirectoryPicker` でフォルダ保存
- 同じく fetch/XHR インターセプタで `audioUrlCache` を埋める

Python 版はブラウザ UI 操作を排除し、**スクロール → cache 読み → `context.request.get`** のシンプルなパイプラインに書き換え。
