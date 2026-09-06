# SUNO 復号方式ダウンロード 実装計画

> **実行者向け**: REQUIRED SUB-SKILL: `codex-driven-development` を使ってタスク単位で実装する。
> （superpowers の計画テンプレートは `subagent-driven-development` と書くが、この
> ワークスペースでは DEV/CLAUDE.md の分業体制により `codex-driven-development` に読み替える。
> 実装は Codex CLI、レビューは Claude サブエージェントが行う。）

**Goal:** SUNO の AES-CTR 暗号化配信に対応し、ワークスペースの全曲を MP3 で取得できる状態に戻す。

**Architecture:** ページに `window.fetch` と `crypto.subtle.decrypt` のフックを注入し、
SPA が初期セグメントを復号した瞬間に鍵とカウンタを捕捉して、保持しておいた暗号文全体を
同じ鍵で再復号する。復号済み MP4 を 1MB ずつ base64 chunk で Python へ転送し、
ffmpeg で MP3 化して保存する。既存の `download_workspace_tracks` の外形は変えない。

**Tech Stack:** Python 3.9+（macOS 既定）、Playwright（sync API）、ffmpeg 8.0.1 / libmp3lame

**設計書:** `docs/superpowers/specs/2026-09-06-suno-fast-decrypt-download-design.md`

**検証に使うワークスペース:** 各タスクの検証コマンドに出てくる `$WS` は、
SUNO の `/me/workspaces` に実在するワークスペース名（例の形式: `159_orzz_260819`）。
Task 3 の検証時に1つ選び、**以降のタスクでは同じものを使う**。シェルでは先に
`WS="<選んだ名前>"` と定義してからコマンドを実行する。曲数が 20 前後のものを選ぶこと。

**参照実装（読むこと）:** `~/dev/suno-fast-ref/page-hook.js` と `~/dev/suno-fast-ref/content.js`
ユーザー自作の Chrome 拡張 suno-fast-v0.10.0 のソース。本計画はこの手法の移植である。
迷ったら必ずこの2ファイルの該当関数を読んで挙動を合わせる。

## Global Constraints

- **絵文字を一切使わない**（コード・ログ・コメント・コミットメッセージ全て）。既存コードに
  絵文字混じりのログがあるが、新規追加分では使わない。
- **`suno_fast_dl.py` は `suno_auto_create.py` を import しない。** 依存は
  `suno_auto_create` → `suno_fast_dl` の一方向のみ。循環 import を避ける。
- **このプロジェクトにテスト基盤は無い。** pytest 等を新設しない。検証は各タスクに書かれた
  検証コマンドの実行と、その出力の貼り付けで行う。
- 既存関数 `download_workspace_tracks(page, workspace_name, target_dir)` の
  **シグネチャと戻り値（成功曲数の int）を変更しない。**
- 旧経路（audio_url 直GET）のコードを削除しない。`APP_SUNO_DL_MODE=legacy` で到達可能に保つ。
- Python は macOS 既定の 3.9 系でも構文エラーにならないこと。`X | None` 形式の型注釈は使わず
  `Optional[X]` を使う。
- 行セレクタは `[data-testid="clip-row"]` を使う（現行コードおよび拡張機能と同じ）。

---

## ファイル構成

| ファイル | 責務 |
|---|---|
| `Python/suno_fast_dl.py`（新規） | 復号キャプチャ方式のダウンロード一式。JS フック文字列、注入、行列挙、1曲取得、MP3変換、統合関数 |
| `Python/suno_auto_create.py`（改修） | `download_workspace_tracks` に `APP_SUNO_DL_MODE` の分岐を追加。フック注入呼び出しを追加。既存実装は温存 |

---

### Task 1: JS フックの実装と注入

**Files:**
- Create: `Python/suno_fast_dl.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `FAST_DECRYPT_HOOK: str` — ページへ注入する JS ソース
  - `install_fast_capture(page) -> None` — `page.add_init_script(FAST_DECRYPT_HOOK)` を呼ぶ
  - `ensure_fast_capture(page) -> bool` — 既に注入済みかを確認し、未注入なら `page.evaluate` で後注入する。注入済み/注入成功で True

**参照:** `~/dev/suno-fast-ref/page-hook.js` の全体。以下を移植する。

移植する要素（page-hook.js の対応関数名を併記）:

1. `window.fetch` の差し替え。`isMediaUrl(url)` が真のレスポンスだけを対象にする。
   判定正規表現は page-hook.js の `isMediaUrl` をそのまま使う。
2. `content-range` を見て、206 かつ全体を覆っていない部分レスポンスは対象外にする
   （page-hook.js の `partialRange` 判定をそのまま移植）。
3. `response.clone()` の body を読み切って暗号文を保持し、先頭64バイトを
   `prefixPromise` として解決する（`readEncryptedBody`）。
4. `crypto.subtle.decrypt` の差し替え。`algorithm.name` が `AES-CTR` のときだけ対象。
   `algorithm.counter` と渡された暗号文の先頭64バイトを控える。
5. 復号結果が `ftyp` で始まるときだけ処理を続ける（`startsWithFtyp`）。
6. 突合は3段階の優先順（`claimPrioritizedRequest`）:
   URLのclipIDが対象songIdと一致 → 同一sessionId → その他。
   各グループ内は先頭64バイトの完全一致（`prefixesMatch`）で確定し、`reserved` で即予約する。
7. 確定した暗号文全体を、同じ `key` と `counter` で `originalDecrypt` に再投入する。
   `CryptoKey` はエクスポートせず、渡されたオブジェクトをそのまま使う。
8. 再復号結果が `ftyp` + `moov` を含むことを検証する（`isInitSegment`）。
9. 結果は **Blob ではなく `Uint8Array` のまま** `window.__sunoFastResults[sessionId]` に格納し、
   `{status:'done', bytes:Uint8Array, size:number, url:string}` の形で保持する。
   失敗時は `{status:'error', error:string}` を格納する。
10. `window.__sunoFastSetContext(context)` を公開する。`{sessionId, songId, title}` を受け取り
    現在のキャプチャ対象として設定する。`null` で解除。
11. `window.__sunoFastGetState(sessionId)` を公開する。
    `{status:'pending'|'done'|'error', received:number, total:number, size:number, error:string}`
    を返す。`bytes` 本体は返さない（サイズが大きいため）。
12. `window.__sunoFastClear(sessionId)` を公開し、保持中の結果と暗号文を解放する。
13. 多重注入防止に `window.__sunoFastDecryptCaptureInstalled` を使う。

**MSE フォールバック（`collectMseChunk` / `exportMse`）は移植しない。** 再生完走が必要で遅い。

14. **鍵のエクスポート可否を1度だけ記録する。** decrypt フック内で、捕捉した `key` に対して
    `crypto.subtle.exportKey('raw', key)` を try/catch 付きで1度だけ試し、
    結果を `window.__sunoFastKeyExportable`（true / false）に格納する。
    **成功しても本実装の挙動は変えない**（設計書 3節）。true だった場合は
    Python 側で AES-CTR 復号して並列ダウンロードできる余地があるため、
    検証時に値を読み取って結果をユーザー報告に含める。

- [ ] **Step 1: `Python/suno_fast_dl.py` を新規作成し、`FAST_DECRYPT_HOOK` を書く**

モジュール冒頭の docstring に「拡張機能 suno-fast-v0.10.0 の page-hook.js から移植」と
参照元パスを明記する。

- [ ] **Step 2: JS の構文チェック**

JS を Python 文字列から取り出して構文検査する。

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 -c "import sys; sys.path.insert(0,'Python'); import suno_fast_dl; open('/tmp/hook.js','w').write(suno_fast_dl.FAST_DECRYPT_HOOK)"
node --check /tmp/hook.js && echo "JS SYNTAX OK"
```

期待: `JS SYNTAX OK`

- [ ] **Step 3: Python の構文チェック**

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 -m py_compile Python/suno_fast_dl.py && echo "PY OK"
```

期待: `PY OK`

- [ ] **Step 4: コミット**

```bash
git add Python/suno_fast_dl.py
git commit -m "SUNO復号キャプチャのJSフックを追加

fetchで暗号文を保持しcrypto.subtle.decryptから鍵とカウンタを捕捉して
全体を再復号する。拡張機能suno-fast-v0.10.0のpage-hook.jsから移植。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 復号結果の Python への転送

**Files:**
- Modify: `Python/suno_fast_dl.py`

**Interfaces:**
- Consumes: Task 1 の `FAST_DECRYPT_HOOK`, `install_fast_capture`
- Produces:
  - `register_transfer_binding(page) -> None` — `page.expose_binding("__sunoFastChunk", handler)` を登録する
  - `fetch_result_bytes(page, session_id) -> bytes` — `window.__sunoFastSendResult(session_id)` を呼んで chunk 転送を起動し、全 chunk 受信後に結合したバイト列を返す

**設計:** 復号結果は 10〜40MB になる。`page.evaluate` の戻り値で一括 base64 を返すと
CDP のメッセージが巨大になるため、1MB ずつ base64 chunk に分割して転送する。
拡張機能 `content.js` の `stageFixedBlob`（1048576 バイト刻み）と同じ手法。

`FAST_DECRYPT_HOOK` 側に `window.__sunoFastSendResult(sessionId)` を追加する。
これは保持中の `Uint8Array` を 1MB ずつ base64 化して
`await window.__sunoFastChunk({sessionId, index, base64, total, size})` を順に呼び、
最後に `{sessionId, index:-1, done:true}` を送る。

Python 側 `register_transfer_binding` のハンドラは、`sessionId` をキーにした辞書へ
chunk を index 順に貯める。`done` を受けたら結合し、**宣言された `size` と
実際のバイト数が一致することを検証する**。不一致なら例外。

- [ ] **Step 1: `FAST_DECRYPT_HOOK` に `__sunoFastSendResult` を追加する**

base64 化は `btoa` に大きな配列を一度に渡すと `Maximum call stack size exceeded` になるため、
8192 バイトずつ `String.fromCharCode` で文字列化してから `btoa` する。

- [ ] **Step 2: `register_transfer_binding` と `fetch_result_bytes` を実装する**

- [ ] **Step 3: 転送経路の単体確認**

SUNO に依存せずに検証する。`about:blank` を開き、フックを注入し、
ダミーの `Uint8Array` を結果として仕込んでから転送させ、往復でバイト列が一致するか確認する。
検証用スクリプトは `Python/scripts/suno_fast_transfer_check.py` として作成する。

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 Python/scripts/suno_fast_transfer_check.py
```

期待する出力:

```
size=3145728 received=3145728 sha256_match=True
TRANSFER OK
```

3MB（chunk が3個以上になる大きさ）のランダムバイト列で、送信前後の sha256 が一致すること。

- [ ] **Step 4: コミット**

```bash
git add Python/suno_fast_dl.py Python/scripts/suno_fast_transfer_check.py
git commit -m "復号結果を1MBずつbase64 chunkでPythonへ転送する経路を追加

巨大なevaluate戻り値を避けるため拡張機能のstageFixedBlobと同じ分割転送にする。
about:blankで往復のsha256一致を確認する検証スクリプトを同梱。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 曲行の列挙（ページネーション対応）

**Files:**
- Modify: `Python/suno_fast_dl.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `iter_workspace_rows(page, status_cb=None) -> List[Dict]`
    各要素は `{"song_id": str, "title": str, "page_no": int}`。ページ順・行順を保つ。

**参照:** `~/dev/suno-fast-ref/content.js` の
`scanCurrentVirtualPage` / `scanPagesOnce` / `goToFirstPage` / `goToNextPage` /
`currentPageNumber` / `findNextPageButton` / `waitForRowsStable` / `readExpectedSongCount`。

現行 `suno_auto_create.py:2152` の `_collect_all_song_uuids` はスクロールのみで
**ページネーションを見ていない**。ここが最大の欠落。

実装方針:

1. `goToFirstPage` 相当で1ページ目へ戻す
2. 1ページ内をスクロールしながら `[data-testid="clip-row"]` を走査し、
   行内の `a[href*="/song/"]` から UUID を、行のテキストからタイトルを取る
3. 行が安定するまで待つ（`waitForRowsStable` 相当。同じ署名が2回続いたら安定とみなす）
4. 次ページボタンがあれば遷移して 2 へ戻る。無ければ終了
5. 重複 song_id は除外する

- [ ] **Step 1: `iter_workspace_rows` を実装する**

- [ ] **Step 2: 実ワークスペースで列挙のみ確認する**

検証スクリプト `Python/scripts/suno_fast_rows_check.py` を作る。
既存の Chrome プロファイルで SUNO を開き、指定ワークスペースの行を列挙して
件数と最初の3件・最後の3件を出力するだけのもの（ダウンロードはしない）。

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 Python/scripts/suno_fast_rows_check.py --workspace "$WS"
```

期待: 出力された件数が SUNO の画面に表示されている曲数と一致すること。
ページが2ページ以上ある場合は `page_no` が 2 以上の行も含まれること。

- [ ] **Step 3: コミット**

```bash
git add Python/suno_fast_dl.py Python/scripts/suno_fast_rows_check.py
git commit -m "ページネーション対応の曲行列挙を追加

現行のスクロール専用列挙は1ページ目しか見ておらず取りこぼす。
拡張機能のscanPagesOnce相当を移植し全ページを走査する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 1曲の取得と MP3 変換

**Files:**
- Modify: `Python/suno_fast_dl.py`

**Interfaces:**
- Consumes: Task 1 の `ensure_fast_capture`、Task 2 の `register_transfer_binding` /
  `fetch_result_bytes`、Task 3 の `iter_workspace_rows`
- Produces:
  - `capture_song(page, song_id, title, timeout_sec=30, status_cb=None) -> bytes`
  - `convert_to_mp3(src_bytes, dest_path) -> None`
  - `allocate_filename(target_dir, title, used_names) -> str`

**capture_song の手順**（設計書 5.6 / 5.7）:

1. song_id で行を引き直す（DOM 参照は仮想リストの再描画で無効化するため毎回引き直す）
2. その行が再生中（一時停止ボタンが出ている）なら prime する
3. `window.__sunoFastSetContext({sessionId, songId, title})` を呼ぶ
4. audio 要素をミュートする
5. 行の再生ボタンを click する。**click の直前と直後の2回ミュートする**
   （拡張機能 `processItem` が `applyMute()` を click の前後で呼んでいるのと同じ）
6. `window.__sunoFastGetState(sessionId)` を 250ms 間隔でポーリングし、
   `status === 'done'` を待つ。`received` が `timeout_sec` 秒間増えなければタイムアウト
7. `__sunoFastSendResult` を呼んでバイト列を受け取る
8. 再生を停止し、`__sunoFastSetContext(null)` と `__sunoFastClear(sessionId)` を呼ぶ
9. 元の音量を復元する

**prime（設計書 5.7）**: 目的の曲が再生中だと click しても fetch が走らない。
拡張機能 `primeAnotherSongIfNeeded` と同じく、別の曲を一度ミュートで再生してから戻る。
これが無いと2曲目以降が高確率で失敗する。

**convert_to_mp3**: 一時ファイルへ書き出し、ffmpeg を実行する。

```
ffmpeg -y -loglevel error -i <src> -vn -c:a libmp3lame -b:a 320k -map_metadata -1 <dest.part>
```

非0終了、または出力が 100KB 未満なら失敗として例外を投げ、一時ファイルを消す。
成功したら `Path.replace` で `dest` へ原子的に移す。

**allocate_filename**: 既存実装と同じ規則。
`re.sub(r'[\\/:*?"<>|]', '_', title)` でサニタイズし、同名があれば
`<title>_2.mp3`, `<title>_3.mp3` と連番。
**この2テイク命名は `app_process_tracks.py:82` が同一グループ判定に使うため厳守。**

- [ ] **Step 1: `convert_to_mp3` と `allocate_filename` を実装する**

- [ ] **Step 2: `capture_song` を実装する**

- [ ] **Step 3: 実SUNOで1曲だけ取得して確認する**

検証スクリプト `Python/scripts/suno_fast_one_check.py` を作る。
指定ワークスペースの先頭1曲だけを取得して `/tmp/suno_fast_check/` に保存する。

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 Python/scripts/suno_fast_one_check.py --workspace "$WS"
ffprobe -v error -show_entries format=duration,bit_rate -show_entries stream=codec_name,channels -of default=nw=1 /tmp/suno_fast_check/*.mp3
```

期待:
- `codec_name=mp3`
- `channels=2`
- `duration` が SUNO の画面に出ている曲尺とおおむね一致する（途中で切れていない）
- `bit_rate` が 320000 前後

- [ ] **Step 4: コミット**

```bash
git add Python/suno_fast_dl.py Python/scripts/suno_fast_one_check.py
git commit -m "1曲の復号キャプチャとMP3変換を追加

再生中の曲は別曲をprimeしてから取り直す。ffmpegで320k MP3へ変換し
既存の2テイク連番命名を維持する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: 統合と既存経路への接続

**Files:**
- Modify: `Python/suno_fast_dl.py`
- Modify: `Python/suno_auto_create.py`（`download_workspace_tracks` の冒頭に分岐を追加）

**Interfaces:**
- Consumes: Task 1〜4 の全て
- Produces:
  - `download_workspace_tracks_fast(page, workspace_name, target_dir, status_cb=None) -> int`

**download_workspace_tracks_fast の流れ:**

0. **前提**: 対象ワークスペースは既に開かれている。ワークスペースを開く処理
   （`_workspace_direct_url` → `_workspace_direct_url_from_status` → `/me/workspaces` から探す）は
   呼び出し側の `suno_auto_create.download_workspace_tracks` が済ませる。
   `suno_fast_dl` は `suno_auto_create` を import できないため、この関数はワークスペースを開かない。
1. `ensure_fast_capture(page)` と `register_transfer_binding(page)`
2. `iter_workspace_rows(page)` で全曲を列挙
3. 1曲ずつ `capture_song` → `convert_to_mp3` → 保存
4. 失敗した曲は `APP_SUNO_DL_RETRIES`（既定2）回まで自動再試行する。
   2回目以降は強制 prime する
5. 進捗は `print` と `status_cb(message, variant)` の両方へ出す。
   既存ログの見た目（`  [3/20] title.mp3 (4.2MB)` 形式）を踏襲する
6. 成功曲数を返す

**環境変数:**

| 変数 | 既定 | 意味 |
|---|---|---|
| `APP_SUNO_DL_MODE` | `fast` | `fast` / `legacy` |
| `APP_SUNO_CAPTURE_TIMEOUT_SEC` | `30` | 1曲あたりの無通信タイムアウト |
| `APP_SUNO_DL_RETRIES` | `2` | 1曲あたりの自動再試行回数 |

`APP_SUNO_PARALLEL_DL` は fast モードでは無視する。legacy モードでは従来どおり有効。

**`suno_auto_create.py` 側の改修:**

`download_workspace_tracks` の、ワークスペースを開き終わった直後
（現行コードで「2) インターセプタが既に install されているか確認」の手前）に分岐を入れる。

```python
    if os.environ.get("APP_SUNO_DL_MODE", "fast").strip().lower() != "legacy":
        import suno_fast_dl
        return suno_fast_dl.download_workspace_tracks_fast(
            page, workspace_name, target_dir,
            status_cb=lambda message, variant="info": _set_status(page, message, variant),
        )
```

これより下の既存コードは一切変更しない。

- [ ] **Step 1: `download_workspace_tracks_fast` を実装する**

- [ ] **Step 2: `suno_auto_create.py` に分岐を追加する**

- [ ] **Step 3: 構文チェック**

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
python3 -m py_compile Python/suno_fast_dl.py Python/suno_auto_create.py && echo "PY OK"
```

期待: `PY OK`

- [ ] **Step 4: 20曲一括で通し確認する**

1 vol 相当のワークスペースに対して既存の入口から実行する。

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
APP_KEEP_BROWSER=0 python3 Python/suno_auto_create.py --download-only \
  --workspace "$WS" --target /tmp/suno_fast_vol
ls -la /tmp/suno_fast_vol/
for f in /tmp/suno_fast_vol/*.mp3; do
  printf "%s " "$(basename "$f")"
  ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$f"
done
```

確認項目:
- 取得成功数が SUNO 側の曲数と一致する
- 同タイトル2テイクが `<title>.mp3` と `<title>_2.mp3` になっている
- 全ファイルの `duration` が 0 でなく、曲尺として妥当な値である
- 別の曲の音声が混入していない（タイトルと曲尺の対応が破綻していない）

`--download-only` 相当の入口が現行 CLI に無い場合は、
`_run_download_only`（`suno_auto_create.py:3887`）を呼ぶ最小のスクリプトを
`Python/scripts/suno_fast_vol_check.py` として作って実行する。

- [ ] **Step 5: legacy 経路が生きていることを確認する**

```bash
cd "/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
APP_SUNO_DL_MODE=legacy APP_KEEP_BROWSER=0 python3 Python/scripts/suno_fast_vol_check.py \
  --workspace "$WS" --target /tmp/suno_legacy_check 2>&1 | head -30
```

期待: 旧経路のログ（`インターセプタ未インストール` や `audio_url 充足` の行）が出ること。
SUNO が暗号化のままなので取得自体は失敗してよい。**経路が生きていることだけを確認する。**

- [ ] **Step 6: コミット**

```bash
git add Python/suno_fast_dl.py Python/suno_auto_create.py Python/scripts/
git commit -m "SUNOダウンロードを復号キャプチャ方式へ切り替え

APP_SUNO_DL_MODE=fastを既定にし、旧audio_url方式はlegacyとして温存する。
download_workspace_tracksの外形は変えないため呼び出し元とパイプラインは無改修。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 完了条件

- 20曲のワークスペースから 20 曲の MP3 が取得できる
- 各 MP3 の duration が妥当で、タイトルと中身が対応している
- `app_process_tracks.py` が従来どおり処理できる
- `APP_SUNO_DL_MODE=legacy` で旧経路に戻せる
