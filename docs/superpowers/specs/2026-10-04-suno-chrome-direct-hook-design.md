# SUNO 作成〜DL: 正式 Chrome 接続と拡張フック直接実行 設計

2026-10-04。対象は Automation Studio の SUNO 自動化（`Python/suno_auto_create.py` 系）。

## 目的

1. `.py` が操作するブラウザを、同梱 Chromium から **Google Chrome 本体 + 自動化専用プロファイル** に切り替えられるようにし、接続先を Automation Studio の設定画面から選べるようにする。
2. DL の復号フックを、Python 内の移植コピーではなく **既存拡張の `page-hook.js` を実行時に読み込んで無改変で注入** する形に変える。
3. 作成フォーム・ワークスペース一覧・曲行のセレクタを、Codex が computer use で読める実 Chrome の現行 SUNO DOM と突合し、ずれている箇所だけ直す。

## 本人の決定（2026-10-04）

- DL は「ストリーム取得のみ」（拡張 v0.10.0 相当）。Studio 書き出し工程は含めない。
- 設定画面に出すのは「ブラウザ接続先」のみ。
- 「正式 Chrome」は Chrome 本体 + 専用プロファイル。普段使いのプロファイルは操作しない。

## 対象外

生成パラメータ（モデル・Exclude・Style Influence・Variety・Weirdness・Max）、ネイティブ Instrumental、DL 設定（方式・タイムアウト・再試行・保存形式）の UI 化、Studio 書き出し、Wobble Day の CUA 手順書（`docs/music/wobble-day-production.md`）と `wobble_suno_bridge.py` の変更。

## 1. ブラウザ接続 `Python/suno_browser.py`（新規）

現在、起動コードは `suno_auto_create.run_browser_automation`、`suno_auto_create._run_download_only`、`suno_queue.launch_browser` の 3 か所に重複している。これを 1 モジュールへ集約する。

### インターフェース

```python
def resolve_browser_settings(settings: dict) -> dict
    # 返り値: {"mode", "profile_dir", "cdp_port", "headless"}（既定値と検証を適用済み）

def open_suno_context(playwright, settings: dict) -> BrowserSession
    # BrowserSession.context : Playwright BrowserContext
    # BrowserSession.owned   : bool（自分で起動したか）
    # BrowserSession.close() : owned のときだけ context を閉じる。cdp では接続だけ切る
```

### モード

| `browser_mode` | 動作 | 既定プロファイル |
|---|---|---|
| `chrome`（既定） | `launch_persistent_context(channel="chrome", ...)` | `~/.config/orzz/chrome_profile` |
| `chromium` | 同梱 Chromium で `launch_persistent_context` | `~/.config/orzz/chromium_profile` |
| `cdp` | `connect_over_cdp("http://127.0.0.1:<port>")`。既存 context の先頭を使う | 接続先任せ |

- 起動引数（`--disable-blink-features=AutomationControlled`、`--no-first-run`、`ignore_default_args=["--enable-automation"]`、viewport 1280x900、`accept_downloads=True`）は現行を引き継ぐ。
- **暗黙のフォールバックはしない。** `chrome` で起動に失敗したら Chromium へ黙って切り替えず、モードと原因を示して失敗させる（プロファイルが分かれるため、切り替わるとログイン状態が変わり原因が見えなくなる）。
- `browser_profile_dir` が空ならモード別既定を使う。`chrome` と `chromium` で同じフォルダを共有させない（Chromium 用プロファイルを Chrome で開くと壊れやすい）。指定パスが他モードの既定と一致する場合はエラー。
- `cdp` では `headless` を無視し、終了時にブラウザ・タブを閉じない。`cdp_port` は 1024〜65535、既定 9222。接続不可なら「Chrome を `--remote-debugging-port=<port> --user-data-dir=<専用フォルダ>` で起動する」旨を案内して失敗させる。
- init script（`_SUNO_AUDIO_URL_INTERCEPTOR`、復号フック、ブランド名、ステータスオーバーレイ）の登録は呼び出し側に残す。`cdp` で既に開いているタブには init script が効かないため、既存の `ensure_fast_capture` の後注入経路で補う。

### ログイン

`chrome` の専用プロファイルは新規のため、初回だけ SUNO へのログインが必要。既存の 300 秒ログイン待ち（`is_suno_logged_in`）をそのまま使う。無人実行時は既存の `UnattendedLoginRequired` で止まる。

## 2. 設定

### 保存

`browser_mode` / `browser_profile_dir` / `cdp_port` をマシン別のグローバル設定（`~/.config/{app_id}/suno_config.json`）に保存する。`app_core._SUNO_GLOBAL_KEYS` と `get_suno_config` の per-channel 無視リストへ 3 キーを追加する（`headless` と同じ扱い）。

### API

`SunoConfigUpdate` に 3 フィールドを追加。`browser_mode` は 3 値のみ、`cdp_port` は範囲検証。不正値は 422。

### 実行時への受け渡し

`/api/suno/start`・`/api/suno/download`・`suno_queue.py` は、起動時に `get_suno_config()` の 3 キーを読む。CLI には `--browser-mode` / `--browser-profile-dir` / `--cdp-port` を追加し、指定があれば設定より優先する。

### UI

`web/static/index.html` の設定画面「SUNO 設定」カードに「ブラウザ接続先」ブロックを追加する。

- モード選択（正式 Chrome / 同梱 Chromium / 起動済み Chrome へ接続）
- プロファイルフォルダ（空欄 = モード別既定。`cdp` 選択時は非表示）
- ポート（`cdp` 選択時のみ表示）
- 初回はログインが必要な旨の説明文

絵文字は使わない。既存カードのクラス・保存処理（`/api/config/suno` への PUT）に合わせる。

## 3. DL: 拡張フックの直接実行

### 読み込み

- 読み込み元の既定は `DEV/Script/suno_fast_m4a_monomono/page-hook.js`（v0.10.0、SHA-256 `8a128e46...2fd08`）。`_claude` リポジトリからの相対で解決し、環境変数 `APP_SUNO_HOOK_PATH` で上書きできる。
- 実行時にファイルを読み、SHA-256 と絶対パスをログへ出す。**内容は改変しない。**
- ファイルが無い・読めない場合は明示エラーで止める。移植コピーへは戻さない。`APP_SUNO_DL_MODE=legacy`（audio_url 方式）は現状のまま残す。
- `suno_fast_dl.FAST_DECRYPT_HOOK` の移植 JS は削除する。

### アダプタ

拡張では `content.js` がフックと `window.postMessage`（`source` 定数つき）で会話している。Python 側は `content.js` の該当部分だけを担う小さなアダプタ JS を持つ。

- 送信: `capture-context`、`mse-capture-on`、`mse-capture-off`、`cancel-session`、`capture-context-clear`、`mse-clear`、`mse-export`
- 受信: `capture-context-ready`、`fast-progress`、`fast-export-result`、`fast-error`
- 受信結果を現行と同じ `window.__sunoFastResults[sessionId]`（`{status, bytes, size, error}`）と進捗へ格納し、`window.__sunoFastSendResult` / `__sunoFastClear` を同じ契約で提供する。

これにより、`register_transfer_binding`・`fetch_result_bytes`・`iter_workspace_rows`・`capture_song`・`convert_to_mp3`・`download_workspace_tracks_fast` と台帳登録は契約を変えずに使い続ける。メッセージの `source` 値・各 `kind` のフィールドは実装時に `page-hook.js` と `content.js` を読んで合わせ、推測で決めない。

## 4. DOM 突合

`codex exec`（computer-use / chrome プラグイン）で、本人の Chrome に開いている SUNO を **読み取り専用** で調べる。Create・削除・設定変更・DL は行わない。

記録対象（`docs/music/suno-dom-snapshot-2026-10-04.md`）:

- `/create`: モードタブ、歌詞ラジオ、歌詞エディタ、Styles 入力、「その他のオプション」と曲名入力、Custom 尺、作成ボタン
- `/me/workspaces`: カード、アーカイブ展開
- `/create?wid=...`: 曲行、曲リンク、再生ボタン、ページ送り、フィルター

各項目について role / aria-label / placeholder / data-testid / 表示テキスト（日英）を記録し、`.py` の現行セレクタと表で突合する。一致は変更しない。不一致だけを修正対象とし、**修正範囲は突合結果を本人へ報告してから確定する**。

## 5. エラー処理

- 起動失敗: モード・プロファイル・原因を 1 メッセージで示す。フォールバックなし。
- フックファイル欠落・ハッシュ読み取り失敗: DL 開始前に失敗させる。
- アダプタが `fast-error` を受けた場合: 現行と同じく曲単位の失敗として再試行回数まで再試行し、不完全取得は例外にする。

## 6. 検証

テスト基盤（`Python/tests/`）あり。新規ロジックは TDD。

| 対象 | 方法 |
|---|---|
| `resolve_browser_settings` | 単体テスト: 既定値、モード別プロファイル、共有禁止、ポート範囲、不正モード |
| `open_suno_context` | Playwright をフェイクにした単体テスト: モード別の呼び出し引数、フォールバックしないこと、`cdp` で close しないこと |
| 設定保存 | 単体テスト: 3 キーがグローバル側へ保存され、per-channel 値で上書きされないこと |
| フック読み込み | 単体テスト: ファイル内容が無改変で渡ること、欠落時のエラー |
| アダプタ | `page-hook.js` 実物 + アダプタをローカル HTML で読み込み、メッセージ往復を Playwright で確認 |
| 既存 | `Python/tests` 全体、`python3 -m py_compile` |
| 実機: 接続 | 3 モードそれぞれで `/create` を開きログイン判定が通る |
| 実機: DL | 既存ワークスペースを DL し、曲数・ffprobe 実尺・全デコードを確認（クレジット消費なし） |
| 実機: フォーム | Create 直前まで投入し、値を読み戻して原稿と一致を確認 |

**実際の Create（クレジット消費）は、実行前に本人へ確認する。**

## 7. 進め方

- ブランチ `feature/suno-chrome-direct-hook`。実装は Codex へ委譲（`codex-driven-development`）。
- `Python/app.py`・`web/static/index.html` には着手前から未コミットの変更（fal-video / layer-motion 関連）がある。今回の変更はそれらと別の箇所に限定し、コミットは今回の hunk だけを `git add -p` 相当で選ぶ。既存の未コミット変更は巻き込まない・消さない。
- 本番デプロイは対象外（ローカルの Automation Studio で動作確認まで）。

## 追記（2026-10-04 本人指示による範囲拡大）

DOM 突合の報告後、本人から「不一致 3 件をすべて直す。時間尺指定や他の数値の調整もできるように、確認の上実装」との指示があり、上の「対象外」のうち生成パラメータの一部を範囲に入れた。

- 追加した項目: 曲の長さ（10〜360 秒）、Weirdness（0〜100）、Style Influence（0〜100）、Variety（0〜4）。範囲と操作方法は `docs/music/suno-dom-snapshot-2026-10-04.md` の 5 節で実画面から採取した。
- 設定画面「SUNO 設定」カードにチャンネル別の設定として追加。空欄は「触れない」（尺は SUNO の Auto のまま）。
- 作成フォームへの投入は読み戻し検証つき。一致しなければ Create の前に止める。
- 引き続き対象外: モデル選択、Exclude Styles の UI 化、BPM（現行の画面では非表示）、ネイティブ Instrumental、DL 設定の UI 化、Studio 書き出し。
- 起動失敗時は成功扱いにせずエラー終了する（計画の当初コードは終了コード 0 だったが、5 節の「失敗させる」に合わせた）。
- SUNO タブの「曲の長さ」セレクト（既定 4:00）は設定画面の値より優先される。設定画面の値は、セレクトで「設定の長さを使う」を選んだときに使われる。
- 実機確認: Automation Studio が chrome モードで起動する自動化専用プロファイルを、普段の Chrome や手動で `--remote-debugging-port` を付けて起動した Chrome で開くと、Cookie の暗号化方式の違いにより SUNO のログイン状態が失われる。cdp モードには chrome モードの専用プロファイルとは別の接続専用フォルダを使い、その Chrome でも SUNO にログインしておく必要がある。
