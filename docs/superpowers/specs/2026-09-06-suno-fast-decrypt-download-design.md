# SUNO ダウンロードの復号方式への差し替え 設計書

作成日: 2026-09-06
対象リポジトリ: caruxx/automation-studio (DEV/_claude)
関連: `Python/suno_auto_create.py`, `Python/suno_queue.py`, `Python/app_pipeline.py`, `Python/app_process_tracks.py`

## 1. 背景

SUNO が音声の配信方式を変更した。従来は `/api/feed/?ids=<uuid>` が返す `audio_url` が
そのまま MP3 の実体を指しており、Cookie を付けて GET すれば取得できた。

現在は CloudFront から **AES-CTR で暗号化された MP4/M4A** が配信され、ページ内の
`crypto.subtle.decrypt` で復号してから MSE で再生する方式になっている。このため
`audio_url` を直接 GET しても暗号文しか得られず、既存のダウンロードは成立しない。

ユーザーが Chrome 拡張機能 `suno-fast-v0.10.0`（SUNOごにょごにょダウンローダー・高速復号版）
を自作しており、その `page-hook.js` / `content.js` に新方式への対応手法が実装されている。
本設計はその中核を Playwright 側へ移植する。

## 2. 現行実装と壊れた箇所

| 関数 | 場所 | 状態 |
|---|---|---|
| `download_workspace_tracks` | suno_auto_create.py:1791 | audio_url 直GET前提。**機能しない** |
| `_parallel_download_files` | suno_auto_create.py:1667 | 同上。**機能しない** |
| `_parallel_download_candidates` | suno_auto_create.py:1746 | 同上。**機能しない** |
| `_collect_all_song_uuids` | suno_auto_create.py:2152 | 行セレクタ `[data-testid="clip-row"]` は現行DOMと一致。**ただしページネーション未対応** |

呼び出し元は `Python/suno_queue.py:347` の1箇所のみ。
`app_pipeline.py` は `music/*.mp3` を glob して後続処理する（app_pipeline.py:785）。
`app_process_tracks.py` は 10箇所以上で `*.mp3` を前提にしている。

## 3. 新方式の原理

拡張機能 `page-hook.js` の手法をそのまま踏襲する。

1. `window.fetch` を差し替え、CloudFront の `/clip/*.m4a|mp4` へのレスポンスを
   `response.clone()` して**暗号文のまま全体をメモリに保持**する。
   同時に先頭64バイトを突合用のprefixとして記録する。
2. `crypto.subtle.decrypt` を差し替える。SPA が初期セグメントを復号した時点で
   `algorithm.counter` と `key`（CryptoKey オブジェクト）を捕捉する。
3. 復号結果が `ftyp` で始まる（= 音声MP4の初期セグメント）ときだけ処理を続ける。
4. 捕捉した暗号文の先頭64バイトと、decrypt に渡された暗号文の先頭64バイトを突き合わせ、
   対応する fetch レスポンスを特定する。
5. **同じ key と counter で、保持していた暗号文全体を再度 `originalDecrypt` にかける**。
   これで曲全体の復号済み MP4 が得られる。
6. 結果が `ftyp` + `moov` を含むこと（初期セグメント形式であること）を検証する。

重要な性質: **曲を最後まで再生する必要はない**。再生ボタンを押して fetch が完走し、
SPA が先頭を復号した時点で、全体の復号がまとめて走る。所要時間はネットワーク速度に律速される。

`CryptoKey` は取り出さずに使い回す。拡張機能が鍵をエクスポートせず同じ CryptoKey で
再 decrypt している事実から、`extractable: false` である可能性が高い。
実装着手時に `crypto.subtle.exportKey` を1度だけ試し、成功するなら Python 側での
並列復号という高速化余地があるため結果を記録する。失敗しても設計は変えない。

## 4. アーキテクチャ

`suno_auto_create.py` は既に 4,233 行あり、これ以上肥大させない。
新規モジュール `Python/suno_fast_dl.py` に切り出す。

```
suno_queue.py
  └─ suno_auto_create.download_workspace_tracks(page, workspace_name, target_dir)
       ├─ APP_SUNO_DL_MODE=fast (既定)  → suno_fast_dl.download_workspace_tracks_fast(...)
       └─ APP_SUNO_DL_MODE=legacy       → 既存実装（そのまま温存）
```

`download_workspace_tracks` のシグネチャと戻り値（成功曲数の int）は変更しない。
呼び出し元とパイプラインは一切改修しない。

**依存の向きを一方向に保つ**: `suno_fast_dl.py` は `suno_auto_create.py` を import しない。
逆向き（`suno_auto_create` → `suno_fast_dl`）のみ許す。循環 import を避けるため、
画面ステータス更新は `suno_auto_create` 側が `_set_status` を束ねた
`status_cb(message, variant)` として渡す。`status_cb` が None のときは `print` のみとする。

### suno_fast_dl.py の構成

| 名前 | 責務 |
|---|---|
| `FAST_DECRYPT_HOOK` (str) | ページに注入する JS。fetch フック / decrypt フック / 突合 / 再復号 / 結果保持 |
| `install_fast_capture(context_or_page)` | `add_init_script` でフックを注入し、転送用バインディングを登録する |
| `iter_workspace_rows(page)` | ページネーション対応の曲行列挙。`{id, title, row_index, page_no}` を返す |
| `capture_song(page, song_id, timeout_sec)` | capture context 設定 → ミュート → 再生 click → 復号完了待ち → バイト列を Python へ転送 |
| `convert_to_mp3(src_bytes, dest_path)` | ffmpeg で MP3 化して保存 |
| `download_workspace_tracks_fast(page, workspace_name, target_dir, status_cb=None)` | 上記を束ねる。戻り値は成功曲数 |

## 5. 詳細仕様

### 5.1 フックの注入

Playwright の `page.add_init_script` / `context.add_init_script` はページのメイン world で
実行される。拡張機能の `world: "MAIN"` 指定に相当する追加処理は不要。

既存の `_SUNO_AUDIO_URL_INTERCEPTOR` と同じく `run_browser_automation` の
コンテキスト生成直後に注入する。既存タブで呼ばれた場合の救済として、
`window.__sunoFastDecryptCaptureInstalled` が false なら `page.evaluate` で後注入する
フォールバックも既存実装に倣って持つ。

### 5.2 対象レスポンスの判定

拡張機能 `isMediaUrl` と同じ判定を使う。

```
/cloudfront\.net\/.*\/clip\/.*\.(?:m4a|mp4)(?:[?#]|$)/i
または /\/clip\/.*\.(?:m4a|mp4)(?:[?#]|$)/i
```

`content-range` が `bytes 0-<end>/<total>` で全体を覆っていない 206 応答は
**捕捉対象から除外する**。プリフェッチの断片を掴むと壊れたファイルになる。

### 5.3 暗号文と復号呼び出しの突合

複数曲を連続処理すると fetch と decrypt が交錯する。拡張機能 `claimPrioritizedRequest`
と同じ3段階の優先順で突合する。この手抜きは**別の曲の音声を保存する事故**に直結する。

1. URL の clip ID が現在の対象 songId と一致する候補
2. 同一 sessionId の候補
3. それ以外

各グループ内では暗号文先頭64バイトの完全一致で確定する（`prefixesMatch`）。
確定した候補は `reserved` フラグで即座に予約し、二重取得を防ぐ。

### 5.4 Python へのバイト列転送

復号結果は 10〜40MB 程度になる。`page.evaluate` の戻り値で一括 base64 を返すと
CDP のメッセージサイズが問題になりうるため、拡張機能 `stageFixedBlob` と同じく
**1MB ずつ base64 chunk に分割して転送する**。

`page.expose_binding("__sunoFastChunk", handler)` を登録し、JS 側から
`await window.__sunoFastChunk({sessionId, index, base64, total})` を順に呼ぶ。
Python 側は sessionId ごとにバッファへ結合する。全 chunk 受信後に
サイズが宣言値と一致することを検証する。

### 5.5 曲行の列挙とページネーション

現行 `_collect_all_song_uuids` はスクロールのみで、ページネーションを見ていない。
拡張機能の `scanPagesOnce` / `goToNextPage` / `goToFirstPage` / `currentPageNumber` に相当する
処理を移植する。

- 行セレクタは `[data-testid="clip-row"]`（現行コードと同じ。変更不要）
- songId は行内の `a[href*="/song/"]` から UUID を抽出する
- 1ページ走査 → 次ページボタンがあれば遷移 → 行が安定するまで待つ、を繰り返す
- ページ遷移後は行の DOM 参照が無効になるため、**行は毎回 songId で引き直す**
  （拡張機能 `locateItemRow` / `validatedItemRow` と同じ）

### 5.6 1曲の取得手順

```
1. 目的の行を songId で特定する
2. その行が「再生中」（一時停止ボタンが出ている）なら prime する（5.7）
3. capture context を設定する（sessionId, songId, title）
4. audio 要素をミュートする
5. 行の再生ボタンを click する
6. 復号完了を待つ（無通信 30 秒でタイムアウト）
7. base64 chunk を受け取り結合する
8. 再生を止め、capture session を解除する
```

### 5.7 prime（既に再生中の曲の再取得）

目的の曲が既に再生中だと、click しても fetch が発生せず取得できない。
拡張機能 `primeAnotherSongIfNeeded` と同じく、**別の曲を一度ミュートで再生してから**
目的の曲に戻る。これが無いと2曲目以降が高確率で失敗する。

自動再試行の2回目以降は、再生状態にかかわらず強制的に prime する
（拡張機能の `forcePrime` と同じ）。

### 5.8 MP3 への変換と命名

- 取得したバイト列を一時ファイル（`.part`）へ書き、ffmpeg で MP3 化する
- コマンド: `ffmpeg -y -i <src> -vn -c:a libmp3lame -b:a 320k -map_metadata -1 <dest>`
  - `-map_metadata -1` は既存 `app_process_tracks.py` の透かし除去と同じ意図
- ffmpeg が非0で終了した場合、または出力が 100KB 未満の場合は失敗として扱い、
  一時ファイルを残さない
- ファイル名は既存実装と同じ規則を維持する
  - `re.sub(r'[\\/:*?"<>|]', '_', title)` でサニタイズ
  - 同名が既にある場合は `<title>_2.mp3`, `<title>_3.mp3` と連番
  - **この2テイク命名は `app_process_tracks.py:82` が同一グループ判定に使っているため厳守**
- 完成した MP3 のみを `target_dir` へ `Path.replace` で原子的に移す

## 6. エラー処理

| 事象 | 挙動 |
|---|---|
| 復号完了待ちが 30 秒無通信 | その曲を失敗とし、自動再試行（最大2回、2回目以降は強制 prime） |
| 再試行を使い切った | その曲をスキップし、次の曲へ進む。最後に失敗曲数を集計して返す |
| 復号結果が ftyp+moov でない | 即失敗（自動再試行しない。形式が違う＝手法が通用していない） |
| 突合できる暗号文が無い | その曲を失敗として扱い、再試行に回す |
| 206 の部分レスポンス | 捕捉対象外。無視する |
| ffmpeg 失敗 | その曲を失敗として扱う。一時ファイルは削除する |
| 行が見つからない（仮想リスト再描画中） | 短い待機を挟んで songId で引き直す。それでも無ければ失敗 |
| ページ全体で1曲も取得できない | ログに DOM 診断を出し、0 を返す（既存挙動と同じ） |

進捗表示は既存の `print` と、`status_cb(message, variant)` 経由の画面ステータス更新の
形式を踏襲する。既存ログの見た目を大きく変えない。

## 7. 設定

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `APP_SUNO_DL_MODE` | `fast` | `fast` = 新方式、`legacy` = 旧 audio_url 方式 |
| `APP_SUNO_CAPTURE_TIMEOUT_SEC` | `30` | 1曲あたりの無通信タイムアウト |
| `APP_SUNO_DL_RETRIES` | `2` | 1曲あたりの自動再試行回数 |

`APP_SUNO_PARALLEL_DL` は fast モードでは無視する（1曲ずつ再生が必要なため）。
legacy モードでは従来どおり有効。

## 8. 検証手順

このプロジェクトにはテスト基盤が無いため、CLAUDE.md の方針に従い
**実行コマンドと出力の貼り付け**で検証する。

### 8.1 構文と静的確認

```
python3 -m py_compile Python/suno_fast_dl.py Python/suno_auto_create.py
```

### 8.2 1曲での確認

実ワークスペースに対して1曲だけ取得し、以下を確認する。

```
ffprobe -v error -show_entries format=duration,bit_rate -show_entries stream=codec_name,channels -of default=nw=1 <出力.mp3>
```

- `codec_name=mp3`、`channels=2`
- `duration` が SUNO の画面表示の曲尺とおおむね一致する（途中で切れていない）
- `bit_rate` が 320000 前後

### 8.3 20曲一括での確認

1 vol 相当（20曲）を通しで取得し、以下を確認する。

- 取得成功数が SUNO 側の曲数と一致する
- 同タイトル2テイクが `<title>.mp3` と `<title>_2.mp3` になっている
- 全ファイルについて `ffprobe` の duration が 0 でない
- `app_process_tracks.py` の処理がそのまま通る（既存の後処理コマンドを実行して確認）

### 8.4 退避経路の確認

```
APP_SUNO_DL_MODE=legacy で起動し、旧経路が従来どおり呼ばれることをログで確認する
（SUNO 側が暗号化のままなら取得は失敗するが、経路が生きていることを確認する）
```

## 9. やらないこと

- 拡張機能 `suno-fast-v0.10.0` 自体の改修（ユーザーの手元ツールとして独立させたままにする）
- アートワークの埋め込み（パイプラインは MP3 のタグを使っていない）
- lamejs の移植（ffmpeg があるため不要）
- MSE フォールバック経路の移植（再生完走が必要で遅い。fast 経路が通れば不要）
- 旧経路の削除（`APP_SUNO_DL_MODE=legacy` で戻せるよう温存する）
- `app_pipeline.py` / `app_process_tracks.py` の改修（MP3 化により不要）

## 10. リスクと対策

| リスク | 対策 |
|---|---|
| SUNO が再び方式を変える | フックを1モジュールに閉じ込め、差し替え範囲を局所化する |
| 別の曲の音声を保存してしまう | 5.3 の3段階突合と先頭64バイト一致を厳守する。検証 8.3 で曲尺を全件確認する |
| 途中で切れたファイルを保存する | 復号結果の ftyp+moov 検証、chunk 総サイズ検証、ffprobe による duration 確認の3段構え |
| 再生音が鳴る | audio 要素を click 前後の2回ミュートする（拡張機能と同じ）。処理後に元の音量へ戻す |
| 拡張機能と Playwright で二重メンテになる | 本設計書に原理を記録し、拡張機能側が更新されたら差分だけ追随する |
