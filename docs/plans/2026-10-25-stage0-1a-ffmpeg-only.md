# 実装計画: 段階 0（保全・P0）と 段階 1a（ffmpeg 専用化・Adobe 削除）

> REQUIRED SUB-SKILL: codex-driven-development（このワークスペースでは subagent-driven-development を読み替える）

- 計画日: 2026-10-25
- 統括: Claude Fable 5.1（本人指定）。実装: Codex CLI。レビュー: Claude サブエージェント（タスク単位）、最終は Opus 5.5 と Sol 6.1 の独立レビュー。
- リポジトリ: `caruxx/automation-studio`（public）。作業 branch `feature/stage1a-ffmpeg-only`、worktree `~/dev/automation-studio-wt/stage1a`、base `1e7ae9fe`（main）。
- 仕様: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §3.1（段階 1a）、分析 `docs/ANALYSIS_YOUTUBE_AUTOMATION_2026-10-25.md` §7.2。
- セキュリティ監査の原本（private）: `~/dev/dev-workspace/archive/_claude/docs/review/2026-09-30/security-audit.md`（S01〜S06）。public repo には写さない。

## Global Constraints

1. 本人確定: PSD・Premiere・AME は使わず将来も使わない。`production_mode` は `ffmpeg_only` のみ有効。旧値 `standard` / `export_engine=ame` は警告ログを出して `ffmpeg_only` として扱う。
2. 秘密情報・メールアドレス・IP・実ユーザー名・実パス（`/Users/<名前>/`、`CloudStorage/GoogleDrive-<メール>`）を追跡ファイルに書かない。既存の記載は削除または `<USER>` / `<DRIVE>` に置換する。
3. 絵文字を新規に書かない（コード・UI・ログ・コミットメッセージ）。既存の print 内の絵文字は触らない（別段階）。
4. 削除は `git rm` で行い、履歴は保持する。削除直前の base には統括が tag `pre-adobe-removal` を打つ（Codex はタグを打たない）。
5. テストは `Python/tests/` の unittest 形式に揃える。実行コマンドは `cd <repo> && APP_CONFIG_DIR=$(mktemp -d) STUDIO_CONFIG_DIR=$(mktemp -d) APP_SHARED_BASE=$(mktemp -d) .venv/bin/python -m unittest discover -s Python/tests -t Python -v`。本物の `~/.config` や Drive に書き込まないこと。
6. サーバー起動・外部通信（SUNO、YouTube、OpenAI、VPS、SSH）をしない。`config/` 配下（git 管理外の稼働データ）を変更しない。
7. 各タスクは 1 コミット以上。コミットメッセージは日本語で「何を・なぜ」、末尾に `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。push しない（統括が許可を得てから行う）。
8. コミット前に `git diff --cached` で Global Constraints 2 の文字列（`@caruvistar`、`/Users/caruvi`、`GoogleDrive-`）が含まれないことを grep で確認し、結果をレポートに貼る。

## 検証の原則

- 各タスクのレポートに「実行コマンド・出力の末尾・成功/失敗数」を貼る。
- `python3 -c "import ast,sys; ..."` 相当で Python 全ファイルの構文確認を行う（`.venv/bin/python -m compileall -q Python`）。
- 構文確認とユニットテストの両方が通らないタスクは DONE にしない。

---

## Task 1: production_mode の一本化と ffmpeg 既定化

**対象ファイル**: `Python/app_core.py`、`Python/app_pipeline.py`、`Python/batch_orchestrator.py`、`Python/app_orchestrator.py`、`Python/app.py`（`:9738` 付近の orchestrator 用チャンネル構築）、`Python/tests/test_adobe_free_production.py`（拡張）、新規 `Python/tests/test_production_mode_resolver.py`。

**要件**

1. `app_core.py` に `effective_production_mode(cfg: dict) -> str` を追加する。戻り値は常に `"ffmpeg_only"`。入力に `production_mode` が `"standard"`、または `export_engine` が `"ame"` のとき、1 回だけ `print` で `WARN legacy production setting ignored: production_mode=<値> export_engine=<値> -> ffmpeg_only` を出す（絵文字なし）。`PER_CHANNEL_KEYS` と既存の `production_mode` 強制（`app_core.py:305-306`）はこの関数経由にする。
2. `app_pipeline._export_engine()` は常に `"ffmpeg"` を返す。`export_engine` 設定は読まない。
3. `app_pipeline.step_psd_composite`（`:1424` 付近）は `production_mode` を見ず、常に Pillow 合成（`app_image_compositor`）を使う。Photoshop 経路（`app_photoshop` の呼び出し）は関数から取り除く（モジュール削除は Task 2）。
4. `app_pipeline.step_premiere` は常に「ffmpeg 経路のため何もしない」で成功を返し、`step_export` は常に `app_ffrender` 経路を使う。AME 経路の分岐と `render_queue` 経由の分岐を取り除く。
5. preflight（`app_pipeline.py:3568` 付近の `needs_premiere`）を削除し、代わりに `ffmpeg`、`ffprobe` の実行可能ファイルと `PIL` の import を確認する `preflight_ffmpeg() -> list[str]`（不足項目の一覧を返す）を追加する。不足があれば exit 78（`EXIT_PREFLIGHT_FAIL`）で止める。
6. `batch_orchestrator.py:222-229` と `app.py` の orchestrator 用チャンネル構築、`app_orchestrator.py:559-579` の Premiere スキップ判定は、`export_engine` を読まず `effective_production_mode()` を使う（結果は常に ffmpeg）。
7. `studio.py` に intent `preflight` を追加する（`routes.json` にも `cli` 型で追加、`requires_vol: false`、`parallelism: {"scope": "per-machine", "max_parallel": 1}`）。`python3 Python/studio.py preflight --json` が `{"ok": true|false, "intent": "preflight", "missing": [...]}` を出す。

**テスト（RED → GREEN の証拠をレポートに）**

- `test_production_mode_resolver.py`: (a) 空 dict → `ffmpeg_only`、(b) `{"production_mode": "standard"}` → `ffmpeg_only` かつ警告 1 回、(c) `{"export_engine": "ame"}` → `ffmpeg_only` かつ警告 1 回、(d) `{"production_mode": "ffmpeg_only"}` → 警告なし。
- `test_adobe_free_production.py` に追加: 設定欠落（`production_mode` も `export_engine` も無い）で `_export_engine() == "ffmpeg"`、`step_psd_composite` が `app_photoshop` を呼ばない（既存の「呼ばれたら失敗」モックを流用）、`preflight_ffmpeg()` が `shutil.which` のモックで不足一覧を返す。

**やらないこと**: モジュールの削除、UI の変更、docs の変更。

---

## Task 2: Adobe 系モジュールとルートの削除（バックエンド）

**前提**: 統括が base に tag `pre-adobe-removal` を打ってから着手する。

**削除対象**（`git rm`）: `Python/app_premiere.py`、`Python/app_photoshop.py`、`Python/jsx_bundle.py`、`Python/app_render_queue.py`、`Python/routers/premiere_photoshop.py`、`Python/YouTube_1080p_Optimized.epr`、`Script/`（.jsx 全部）、`cep_extension/`、`uxp_plugin/`。

**要件**

1. `Python/app.py` から `routers.premiere_photoshop` の include と、`render_queue` の startup フック・`/api/render-queue*` の 5 route・`/api/export*` のうち AME 依存の route を削除する。`/api/health` の `render_queue` 件数は削除する。削除する route の一覧をレポートに書く。
2. `Python/app_core.py`、`app_pipeline.py`、`app_orchestrator.py`、`batch_orchestrator.py`、`app_timeline.py`、`routers/timeline.py`、`routers/video_edit.py`、`app_vlogrender.py` などから、削除モジュールへの `import` と参照を取り除く。`grep -rn -E "app_premiere|app_photoshop|jsx_bundle|app_render_queue|premiere_photoshop|pymiere|CSInterface|do javascript" Python web skills docs *.md *.sh pyproject.toml` の結果が、`docs/ANALYSIS_*.md`・`docs/SPEC_*.md`・`docs/plans/*.md`・`archive` を除いて 0 件になること（docs の本文は Task 4）。
3. `Python/routes.json` から Adobe 依存の intent（premiere、export の AME 版、psd、photoshop 系）を削除し、`composite` と `render` に相当する intent は `app_pipeline.py {vol} --only psd_composite` / `--only export` を指す既存のものを残す（工程名の改名は別段階）。
4. `Python/settings_catalog.json` から `channel.export_engine`、`template_prproj`、`template_psd`、Premiere/Photoshop/AME 固有のキーを削除する。`channel.production_mode` は `choices: ["ffmpeg_only"]` にする。`settings_service.py` のバリデーションが追従すること。
5. `pyproject.toml` から `pymiere` を外し、説明文の「Premiere / Photoshop」を削る。
6. `setup_launchd.sh`、`Python/start.sh`、`package.sh`、`package_exclude.txt` の Adobe 関連の記述（CEP インストール、PlayerDebugMode）を削除する。
7. `.venv/bin/python -m compileall -q Python` が通り、Task 1 のテストを含む全テストが通る。

**やらないこと**: `web/static` の変更（Task 3）、docs の本文変更（Task 4）。

---

## Task 3: フロントエンドから Adobe 関連 UI を外す

**対象**: `web/static/index.html`、`web/static/js/assembly-premiere-layout.js`、`web/static/css/assembly-premiere-layout.css`、`web/static/sw.js`。

**要件**

1. Task 2 で削除した API（`/api/premiere/*`、`/api/photoshop/*`、`/api/render-queue*`、`/api/export` の AME 版、`/api/psd/*`）を呼ぶ関数・ボタン・タブ・設定欄を `index.html` から削除する。削除後、`grep -n -E "api/(premiere|photoshop|render-queue|psd)" web/static` が 0 件。
2. `assembly-premiere-layout.js` / `.css` を `git rm` し、`index.html` の `<script>` / `<link>` 参照と `sw.js` のキャッシュ一覧から外す。`sw.js` の `CACHE_NAME` を `v5` に上げる。
3. 設定画面（`e2-ui.js` が `settings-catalog` から描画する）は Task 2 のカタログ変更に追従するので、index.html 側に残る `export_engine` / `template_prproj` / `template_psd` の直書き入力欄を削除する。
4. 削除した関数を参照する `onclick` が残っていないこと: `node -e` で index.html の `onclick="..."` を抽出し、同ファイル内に定義の無い関数名を列挙するスクリプトを `Python/scripts/check_onclick_refs.js` として追加し、結果 0 件をレポートに貼る。
5. ブラウザでの実画面確認は統括が行う。Codex はサーバーを起動しない。

---

## Task 4: 文書・skills・AGENTS.md の更新と個人情報の除去

**対象**: `README.md`、`OVERVIEW.md`、`SPEC.md`、`AGENTS.md`、`CLAUDE.md`（repo 内）、`SECURITY_NOTES.md`、`skills/app-premiere.md`、`skills/app-psd-composite.md`、`skills/app-export.md`、`skills/app-workflow.md`、`skills/pipeline.md`、`skills/video.md`、`docs/QUOTA_AUDIT_APPLICATION.md`、`docs/XSERVER_RESTORE_2026-09-06.md`、`docs/superpowers/plans/2026-09-06-suno-fast-decrypt-download.md`、`UX_REVIEW_2026-07.md`、`package.sh`。

**要件**

1. Adobe 必須の記述（README の動作要件、OVERVIEW の 11 工程のうち psd/premiere/export の説明、SPEC の API 一覧の該当分）を ffmpeg 専用の記述に置き換える。`skills/app-premiere.md`、`skills/app-psd-composite.md` は `git rm`。`skills/app-export.md` は ffmpeg 書き出し（`app_ffrender`）の説明に書き換える。
2. `AGENTS.md:367` の Drive 絶対パス（メールアドレスを含む）、`AGENTS.md:28` と `CLAUDE.md:17` の launchd 記述の矛盾（「2026-07-04 廃止・手動起動」に統一）、`AGENTS.md:375` の Codex 委譲テンプレートの repo 指定（Drive の `_claude` → `~/dev/automation-studio`）を直す。
3. `docs/XSERVER_RESTORE_2026-09-06.md` は VPS の IP・SSH 鍵名・配信チャンネル名を含むため `git rm` する（統括が private の `dev-workspace/archive` へ退避済みであることを確認してから）。`docs/QUOTA_AUDIT_APPLICATION.md` と `docs/superpowers/plans/2026-09-06-*.md`、`UX_REVIEW_2026-07.md` のメールアドレス・`/Users/<名前>/` を `<USER>` / `<DRIVE>` に置換する。`package.sh:68` のユーザー名リテラルは環境変数 `$USER` に置き換える。
4. 完了後 `git grep -n -E "@caruvistar|/Users/caruvi|GoogleDrive-abe" -- . ':!docs/ANALYSIS_*' ':!docs/SPEC_*' ':!docs/plans/*'` が 0 件。
5. `SECURITY_NOTES.md` の AceDataCloud / Midjourney / Flow の節は「撤去済み（2026-06-07 D8）」の 1 行に置き換える。

---

## Task 5: 依存の固定と python-multipart の更新（S06）

**対象**: `pyproject.toml`、新規 `requirements.lock`（`pip freeze` 出力）、`Python/start.sh`、`Python/tests/`。

**要件**

1. `pyproject.toml` の依存を次に更新する: `python-multipart>=0.0.30`、`fastapi>=0.115`、`starlette` は fastapi 追従、`pillow>=10`、`numpy>=1.26`、`playwright>=1.45` を本体依存へ移動。`requires-python = ">=3.12"`。
2. `[tool.pytest.ini_options] testpaths = ["Python/tests"]` に修正し、`pytest` でも unittest 形式のテストが拾えることを確認する（`python -m pytest -q Python/tests`）。
3. `Python/start.sh` の `pip3 install --user` 方式を、`.venv` が無ければ作成して `requirements.lock` から入れる方式に変える。`kill -9` によるポート奪取（`start.sh:69-71`）は削除し、使用中なら exit 1 でメッセージを出す。
4. `requirements.lock` を `.venv` の `pip freeze` から生成してコミットする。

---

## Task 6: 任意ファイル配信の封じ込め（S02、S03）

**対象**: `Python/routers/video_edit.py`、`Python/routers/images.py`、`Python/app_core.py`（path helper）、新規 `Python/tests/test_path_boundary.py`。

**要件**

1. `GET /api/video-edit/preview` は `path` クエリを受け付けず、`video_name`（`validate_video_name` で検証）と `asset`（ファイル名のみ、`..` と区切り文字を拒否）で解決する。解決結果は `os.path.realpath` で登録チャンネルフォルダ配下にあることを確認し、拡張子を `mp4 / mov / m4a / mp3 / png / jpg / jpeg / webp` に限定する。
2. `routers/images.py` の `video_folder` 受け付け（`:50-59, 251-280, 305-355`）を `video_name` ベースに変更し、同じ realpath 検証を通す。Canvas 保存・テンプレート保存の出力先は登録チャンネルフォルダ配下に固定する。
3. `app_core.py` の共通 path helper は、許可 root と「同じ prefix を持つ隣接パス」（例: `/a/b` と `/a/bc`）を拒否し、symlink は realpath 後に root 配下であることを要求する。
4. テスト: チャンネル外パス・`..`・隣接 prefix・symlink 脱出・許可外拡張子で 400/403、正常素材で 200 相当（FastAPI TestClient、一時ディレクトリ）。

---

## Task 7: 分析 URL 取得の検証（S04）

**対象**: `Python/app.py:4999-5034`（`POST /api/analysis/thumbnails`）、`Python/app_benchmark_analyze.py:450-522`、新規 `Python/safe_fetch.py`、新規 `Python/tests/test_safe_fetch.py`。

**要件**

1. `safe_fetch.fetch_image(url, max_bytes=5_000_000) -> bytes` を新設。`https` のみ、ホスト名解決後の全 IP が loopback / private / link-local / metadata（169.254.0.0/16）でないこと、redirect は各 hop で同じ検証、最終 Content-Type が `image/*`、容量上限を検証する。違反は `SafeFetchError`。
2. `/api/analysis/thumbnails` の `urls` はこの関数経由に限定し、`uploaded_paths` の受け付けを廃止して、`POST /api/analysis/uploads`（multipart、画像のみ、一時領域へ保存し upload ID を返す）に置き換える。
3. テスト: 許可 URL（モックサーバー）成功、`http`、loopback、private IP、redirect 先 private、`text/html`、容量超過で拒否。

---

## 段階 0 のうち本計画に含めないもの（統括が別途扱う）

- S01（loopback 認証免除）と S05（Codex CLI の sandbox 解除）: Mac ローカル運用の動線（UI と画像生成）を変えるため、段階 3（VPS 正本化）と段階 5（サムネ AI 選定）で設計し直す。本計画では触らない。
- Drive 監査資料と `config_backups` の退避: 統括が実施済み（`~/dev/dev-workspace/archive/...` と ローカル `Application Support`）。
- 未統合 branch `feature/suno-chrome-direct-hook` の統合: 本人の判断待ち。
- git 履歴に残る個人情報の除去（force push が必要）: 行わない。
