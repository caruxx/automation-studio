# Automation Studio — Claude Code 向け運用ガイド

## Codex 単独実行モード

`codex exec "vol3を作って"` のような短い指示でも、Codex は会話履歴に頼りすぎず、次の順番でローカル文書を確認してから実行する。

1. `AGENTS.md`
2. `skills/<domain>.md`
3. `docs/ARCHITECTURE_MAP.md`
4. `docs/RUNBOOK_VOL.md`

vol 制作を一気通貫で進める場合は、まず該当 domain の `skills/<domain>.md` を読み、全体構造や step の分岐に迷ったら `docs/ARCHITECTURE_MAP.md`、実作業のチェックリストは `docs/RUNBOOK_VOL.md` を正とする。チャンネル固有ルールがある場合は `docs/CHANNELS/<channel>.md` も確認する。

単一リソース操作は必ず opt-in ロックを通す。SUNO / Premiere / Photoshop を直接叩かず、次の形で包む。

```bash
python3 Python/parallel_guard.py suno-auto -- python3 Python/studio.py suno-auto --vol <N> --count 10
python3 Python/parallel_guard.py psd -- python3 Python/app_pipeline.py <N> --only psd_composite
python3 Python/parallel_guard.py premiere -- python3 Python/app_pipeline.py <N> --from premiere
```

実行前チェック:

```bash
pgrep -f suno_auto_create || true
curl -fsS http://localhost:8888/api/config/migration-status >/dev/null
```

- `pgrep -f suno_auto_create` で既存 SUNO 実行が見つかったら、同時実行せず完了を待つ。
- port 8888 が応答しない場合は `bash Python/start.sh` で起動してから再確認する(launchd 常駐は 2026-07-04 に廃止済み。Google Drive File Provider 制約のため手動起動運用)。
- ネットワーク断や一時的な接続拒否は即失敗扱いにせず、`curl` が 200 を返すまで待って再試行する。

## 正規入口

AI / 人間の正規の入口は `_claude` ルートから `python3 Python/studio.py`。
個別コマンドを直接実行する前に、まず `python3 Python/studio.py <intent> --vol <N> --dry-run` で解決結果と実行コマンドを確認する。
機械可読な真実は `Python/routes.json`。下の自然言語対応表は概要として残す。

## クイックリファレンス

スクリプトフォルダ:
```
cd <_claudeのルートパス>/Python
```

## 一括パイプライン（推奨）

```bash
# vol.78 を全工程実行（SUNO → リネーム → Premiere → 書き出し → メタ → アップロード）
python3 app_pipeline.py 78

# Web API 経由で実行（localhost:8888 起動中）
python3 app_pipeline.py 78 --via-api

# Premiere 以降だけ再実行
python3 app_pipeline.py 78 --from premiere

# メタデータだけ生成
python3 app_pipeline.py 78 --only meta

# 確認だけ
python3 app_pipeline.py 78 --dry-run
```

## 個別操作ワンライナー

### フォルダ作成
```bash
curl -s -X POST http://localhost:8888/api/videos/create \
  -H 'Content-Type: application/json' \
  -d '{"publish_date":"2026-04-20"}'
```

### SUNO 楽曲生成（Claude CLI、一括モード、20曲）
```bash
python3 suno_auto_create.py \
  --prompt "lounge jazz BGM, elegant cafe atmosphere" \
  --count 10 --interval 15 --provider claude --batch \
  --workspace vol_vol78
```

### SUNO 楽曲生成 → DL → 後処理（一気通貫）
```bash
python3 studio.py suno-auto --vol 78 --prompt "lounge jazz BGM, elegant cafe atmosphere" --count 10
```

### SUNO 楽曲ダウンロード
```bash
python3 suno_auto_create.py \
  --download-workspace vol_vol78 \
  --download-dir "/path/to/78_vol_260420"
```

### 楽曲リネーム（タイトルのみ、ffmpeg なし）
```bash
python3 app_process_tracks.py /path/to/78_vol_260420 --rename-only
```

### 楽曲後処理（リネーム + ffmpeg フェードアウト + ゲイン正規化）
```bash
python3 app_process_tracks.py /path/to/78_vol_260420
```

### AI メタデータ生成（タイトル・説明・タグ）
```bash
# タイトル 5 候補
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"titles","count":5}'

# 説明文
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"description"}'

# タグ
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"tags"}'
```

### タイトル保存
```bash
curl -s -X PUT http://localhost:8888/api/videos/78_vol_260420/title \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","new_title":"Golden Hour Reverie"}'
```

### タグ保存
```bash
curl -s -X PUT http://localhost:8888/api/videos/78_vol_260420/tags \
  -H 'Content-Type: application/json' \
  -d '{"tags":["BGM","Lounge","Chill","Jazz","AI Music"]}'
```

### 背景画像生成（ベンチマーク参照 + チャンネルコンセプト）
```bash
# Web API 経由
curl -s -X POST http://localhost:8888/api/bgimage/run \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","ref_count":3,"force":false}'

# CLI 直接（パイプライン step だけ実行）
python3 app_pipeline.py 78 --only bgimage

# 強制再生成（既存 vol{N}.png/.jpg を上書き）
APP_BGIMAGE_FORCE=1 python3 app_pipeline.py 78 --only bgimage
```

詳細: [skills/app-bgimage.md](skills/app-bgimage.md)

### Premiere 自動配置（3 時間、プロジェクト自動オープン）
```bash
curl -s -X POST http://localhost:8888/api/premiere/run \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","duration_h":3,"duration_m":0,"duration_s":0}'
```

### 画像のみ後から配置
```bash
curl -s -X POST http://localhost:8888/api/premiere/place-images \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420"}'
```

### YouTube アップロード（限定公開）
```bash
curl -s -X POST http://localhost:8888/api/youtube/upload \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","privacy":"unlisted"}'
```

### 競合分析（スプレッドシート経由、API quota ゼロ）
```bash
# 分析のみ
python3 app_competitor.py --analyze

# 分析 + vol.78 向け提案
python3 app_competitor.py --propose 78

# ホットチャンネル TOP10 を API で取得
curl -s http://localhost:8888/api/analysis/hot-channels?top_n=10
```

### Web サーバー起動 / 停止
```bash
# launchd 常駐時: コードミラー再同期 + 再起動
bash setup_launchd.sh --sync

# launchd 状態確認
launchctl list | grep automation

# 非常駐時: 直接起動
bash Python/start.sh

# 停止
lsof -ti:8888 | xargs kill -9
```

## ユーザーからの自然言語指示 → 実行マッピング

| ユーザーの言い方 | 実行すべきこと |
|----------------|-------------|
| 「vol.78 を作って」 | `curl POST /api/videos/create {"publish_date":"..."}` |
| 「新しい動画を作って」 | 下の「新規動画の企画ルール(seed分析の反映)」を確認してから、必要に応じて `curl POST /api/videos/create {"publish_date":"..."}` |
| 「vol.78 の楽曲を作って」 | `python3 suno_auto_create.py --workspace vol_vol78 ...` |
| 「生成からDL、後処理まで自動でやって」 | `python3 studio.py suno-auto --vol 78 --prompt "..." --count 10` |
| 「楽曲をダウンロードして」 | `python3 suno_auto_create.py --download-workspace vol_vol78 ...` |
| 「リネームして」 | `python3 app_process_tracks.py <folder> --rename-only` |
| 「後処理して」 | `python3 app_process_tracks.py <folder>` |
| 「背景画像を作って」 | `curl POST /api/bgimage/run {"video_name":"..."}` または `python3 app_pipeline.py <vol> --only bgimage` |
| 「サムネを Photoshop で作って」「PSD を合成して」 | `python3 app_pipeline.py <vol> --only psd_composite`（bgimage 後・premiere 前。`<vol_folder>/vol{N}.jpg` + `サムネイル.jpg` を 2 枚出し） |
| 「サムネを AI で作って」「サムネを自動生成して」 | `python3 app_pipeline.py <vol> --only thumbnail`（ベンチマーク分析 concept/visual_direction から**プロンプトを動的構築** → codex で生成 → `thumbnail.png` に昇格。`APP_THUMBNAIL_PROVIDERS=flow` 指定時も警告のうえ codex にフォールバック。詳細 [skills/app-thumbnail.md](skills/app-thumbnail.md)。⚠ 既に `サムネイル.jpg`(PSD合成)があるとスキップ） |
| 「参照画像フォルダを変更して」 | Web UI → 設定タブ → **参照画像フォルダ（背景画像生成）** → フォルダパス欄を編集 → 保存（`.app_channel_config.json` の `reference_image_dir` に per-channel 保存。空欄なら Picked → rival thumbs にフォールバック） |
| 「タイトルを提案して」 | `curl POST /api/videos/.../suggest {"mode":"titles"}` |
| 「Premiere で配置して」 | `curl POST /api/premiere/run {"video_name":"..."}` |
| 「書き出して」 | `curl POST /api/premiere/export` |
| 「アップロードして」 | `curl POST /api/youtube/upload {"video_name":"..."}` |
| 「全部やって」 | `python3 app_pipeline.py <vol>` |
| 「Premiere からやり直して」 | `python3 app_pipeline.py <vol> --from premiere` |
| 「競合を分析して」 | `python3 app_competitor.py --analyze` |
| 「seed動画を分析して」 | `python3 Python/studio.py seed-analyze --url <動画URL>` または `curl POST /api/benchmark/seed/run` |
| 「競合分析から提案して」 | Web UI「AI 提案（ベンチマーク連動）」パネル、または `curl POST /api/videos/.../suggest-all`（競合分析を内部で反映。旧 suggest-with-analysis は廃止） |
| 「今伸びてるチャンネルは？」 | `curl GET /api/analysis/hot-channels?top_n=10` |
| 「WEB を起動して」 | launchd 常駐時は `bash setup_launchd.sh --sync`、非常駐時は `bash Python/start.sh` |
| 「WEB を再起動して」 | launchd 常駐時は `bash setup_launchd.sh --sync`、非常駐時は `lsof -ti:8888 | xargs kill -9; bash Python/start.sh` |
| 「ライブ配信を開始して/止めて」 | `curl POST /api/live/streams/<id>/start`（stop/restart も同形）。状態は `curl GET /api/live/status` |
| 「ライブの動画を差し替えて」 | 設定変更後 `curl POST /api/live/streams/<id>/swap`（プレイリスト次へは `?next=1`）。**配信は止まらない** |
| 「ライブ配信の調子は？」「VPS の負荷は？」 | `curl GET /api/live/status`（load/mem/送信Mbps/グループ別容量 + 配信別稼働）。ログは `GET /api/live/streams/<id>/log` |
| 「何人見てる？」 | `curl GET /api/live/viewers?force=1`（同時視聴者数。5分キャッシュ） |
| 「ライブのタイトル/サムネを変えて」 | `GET /api/live/broadcasts?stream_id=<id>` で video_id → `PUT /api/live/broadcasts` / `POST /api/live/thumbnail` |
| 「VPS を初期設定して」 | Web UI「ライブ配信」→ 初期設定。CLI なら `curl POST /api/live/setup {host, password?}`（詳細 [skills/app-live-streaming.md](skills/app-live-streaming.md)） |

### 新規動画の企画ルール(seed分析の反映)

「新しい動画を作って」と依頼されたら、フォルダ作成の前に seed 分析を確認する:
1. `python3 Python/app_benchmark_seed.py --list` で保存済み seed 分析を確認（空なら通常フローでよい）
2. 最新 seed の `pdca_hypothesis.changed_element` を今回の1本で検証する変更点として採用（変えるのは1要素だけ）
3. `do_not_copy` / `risk_notes` の要素は企画に使わない
4. メタ生成(meta step)・提案(suggest-all)・SUNOプロンプト提案には seed 分析が自動注入される（claude_proposer / app_benchmark_analyze 経由）ので、手動で貼り込む必要はない

## vol 番号からフォルダ名の解決

フォルダ名は `{vol}_{prefix}_{YYMMDD}` 形式。API は `video_name` で受け付ける。
vol 番号だけ言われた場合は `/api/videos` をクエリして該当フォルダ名を確認:

```bash
curl -s http://localhost:8888/api/videos | python3 -c "
import json,sys
vs = json.load(sys.stdin).get('videos',[])
for v in vs:
    if v['num'] == '78':
        print(v['name'])
        break
"
```

## エラーリカバリ手順

### SUNO 関連

| エラー | 対処 |
|--------|------|
| ブラウザが起動しない | `python3 -m playwright install chromium` |
| SUNO にログインできない | ブラウザが開いたら手動でログイン → 待機後に続行 |
| Workspace 作成失敗 | SUNO のUIが変わった可能性。手動で `/me/workspaces` から作成 |
| DL で audio_url 取得失敗 (cache miss) | コンテキスト再起動: Playwright を閉じて `--download-workspace` を再実行 |
| 全曲同じタイトル | `--batch` を付けて一括生成。プロンプトに diversity 指示を追加 |

### リネーム関連

| エラー | 対処 |
|--------|------|
| 「既に一致」で何もリネームされない | サムネなし + ペルソナ空。設定画面でペルソナを入力、または vol*.jpg を配置 |
| タイトル取得数が少ない | チャンク分割 (10件ずつ) で再試行される。ログで `⚠️ チャンク N 取得失敗` 確認 |
| ffmpeg エラー | `brew install ffmpeg` でインストール確認 |

### Premiere 関連

| エラー | 対処 |
|--------|------|
| 「Premiere Pro 上で実行してね」 | `video_name` 付きで API を叩く（.prproj 自動オープン）。Premiere が起動していることを確認 |
| Pymiere 接続失敗 | Premiere Link パネルがインストールされているか確認: `bash cep_extension/install.sh` |
| 画像が見つからない | 新仕様は alert せず音声のみ配置。後から `place-images` で追加可 |

### YouTube 関連

| エラー | 対処 |
|--------|------|
| OAuth 認証エラー | `python3 app_youtube.py --auth-only` で再認証 |
| タグがハードコード | `youtube_tags.txt` が存在すればそちらを使用。なければ既定タグにフォールバック |
| MP4 が見つからない | Premiere → 書き出しを先に完了させる |

### 一般

| エラー | 対処 |
|--------|------|
| Web サーバーが応答しない | launchd 常駐時は `launchctl list | grep automation` で状態確認後 `bash setup_launchd.sh --sync`。非常駐時は `lsof -ti:8888 | xargs kill -9; bash Python/start.sh` |
| パイプライン途中で止まった | `python3 app_pipeline.py <vol> --from <止まった工程>` で再開 |
| Claude CLI がない | `which claude` で確認。無ければ Claude Code CLI をインストール |

## 復旧・再開の鉄則（Codex / Claude 共通・必読）

vol128/131（2026-07-12）の実事故から定めた約束事。**どのエージェント（Codex CLI / Claude / 人間）がどの工程を引き継いでも、以下を必ず守る。**

### SUNO 復旧の鉄則
1. **手動 DL の置き場所はフォルダ直下**（`music/` に直接置くことは禁止）。
   `music/` に直接置くと rename step が「処理済み」と誤認してスキップし、
   生ファイル名（`〜_2` 付き）・同曲2テイク重複・フェード/音量正規化なしのまま動画化される。
   ```bash
   # 正: vol フォルダ直下に DL → rename step が選曲/リネーム/フェードを行う
   python3 suno_auto_create.py --download-workspace <ws> --download-dir "<vol_folder>"
   python3 app_pipeline.py <N> --from rename
   ```
2. **「生成リクエスト送信 = 完了」ではない**。suno step がタイムアウトしても曲は SUNO 側でレンダリングが続いている。
   時間をおいて DL だけ再実行すれば回収できる（workspace は残っている）。
3. suno step の絶対タイムアウトは `APP_SUNO_STEP_TIMEOUT` で上書き可（既定はレンダ待ち 2700s を含む値に修正済み）。

### 再レンダ（export やり直し）の鉄則
4. music/ の中身を差し替えたら、**再 export 前に必ず stale キャッシュを消す**:
   ```bash
   rm -f "<vol_folder>/.ffrender_manifest.json"
   rm -rf /tmp/ffrender/<vol_folder_name> ~/.cache/ffrender/<vol_folder_name>
   ```
   旧 manifest が残っていると「曲順が空（music/ 解決に失敗）」で export が落ちる、
   または旧曲リストのまま静かにレンダされる。

### 検証の鉄則
5. **step の「OK」表示を信用しない。アップロード前に実体で裏取りする**:
   - `music/` に想定曲数があるか・ファイル名に `_2` が残っていないか
   - `youtube_description.txt` の Tracklist に `_2` や重複曲が無いか
   - `ffprobe` で尺・コーデック
   - `youtube_upload.json` の `schedule` / `privacy`
6. 0 曲は失敗（`app_process_tracks.py` は素材ゼロで exit 1 に修正済み。silent OK に戻さない）。

### インストゥルメンタル系の生成数（標準）
7. **SUNO 生成リクエストは 10 曲 = 20 テイク**（SUNO は 1 リクエスト 2 テイク）。
   `suno.keep_both_takes=true` で 20 テイク全部を候補にし、そこから選曲する（捨てテイクの無駄を出さない）。
   尺が足りない分はタイムラインが一巡後ループして目標尺まで引き伸ばすので、長尺チャンネルも同じ 10 リクエストで良い（ユーザー承認済み 2026-07-12）。
   適用済み: orzz / w_workspace / harbor_notes / sukima / ragtime_whiskers（loop_count=10, interval=15s, keep_both_takes=true）。
8. **送信間隔は 15 秒を下限とする**（Cloudflare ボット判定対策。これ未満に詰めない。`APP_KEEP_BROWSER=1` 既定も維持）。

### チャンネル切替の鉄則
9. `studio.py` 実行時は **active channel の表示を必ず確認**。suno-auto の workspace は
   `{channel_prefix}_vol{N}`（routes.json 修正済み）。チャンネルをまたぐ作業の前に `--channel <id> --switch` を明示。

## 仕様書・スキル参照

- [SPEC.md](SPEC.md) — 全体仕様（API 一覧・データ契約・アーキテクチャ）
- [skills/app-workflow.md](skills/app-workflow.md) — 11 工程の全体フロー（plan→suno→rename→bgimage→psd_composite→premiere→export→qa→meta→localization→thumbnail→upload）
- [skills/app-bgimage.md](skills/app-bgimage.md) — 背景画像生成（参照画像で寄せる固定テンプレ寄り）
- [skills/app-thumbnail.md](skills/app-thumbnail.md) — AI サムネ生成（ベンチ分析から動的プロンプト構築 → codex で生成。Flow 経路は廃止）
- [skills/app-web-dashboard.md](skills/app-web-dashboard.md) — Web UI / API / History API
- 個別スキルは [skills/](skills/) ディレクトリ内の各 `.md` を参照

## Codex への委譲テンプレ

Codex に作業を渡すときは、会話履歴に依存しすぎず、次の情報を 1 つの指示にまとめる。

```md
# 作業対象
repo: /Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude
domain: <music|image|video|publish|analysis|pipeline|web>
参照 skill: skills/<domain>.md

# 目的
<何を直す/調べる/作るかを 1-3 文で書く>

# 正規入口
まず `python3 Python/studio.py <intent> --vol <N> --dry-run` で解決結果を確認。
直接 CLI が必要な場合も、dry-run の結果と差分を説明してから実行。

# 禁止事項
- 推測で仕様を書かない。コードを読んで根拠を確認する。
- 既存ファイルの削除・大規模書き換えをしない。
- credentials / token / secret をログやドキュメントに保存しない。
- SUNO / Premiere / Photoshop の同一リソースを同時実行しない。
- 既存のユーザー変更を revert しない。

# 並列・ロック
単一リソースを触る場合は必要に応じて opt-in ラッパを使う:
- SUNO: `python3 Python/parallel_guard.py suno -- <cmd...>`
- Premiere/AME: `python3 Python/parallel_guard.py premiere -- <cmd...>`
- Photoshop: `python3 Python/parallel_guard.py psd -- <cmd...>`

# 検証
<実行するコマンド/API/ファイル確認を列挙>

# 報告形式
- 変更/成果物パス
- 実行した検証と結果
- 見つけた不整合・残リスク
```

ドメイン別の最低限の前提・入口・並列可否は以下を参照する。

| domain | 参照 skill |
|---|---|
| music | [skills/music.md](skills/music.md) |
| image | [skills/image.md](skills/image.md) |
| video | [skills/video.md](skills/video.md) |
| publish | [skills/publish.md](skills/publish.md) |
| analysis | [skills/analysis.md](skills/analysis.md) |
| pipeline | [skills/pipeline.md](skills/pipeline.md) |
| web | [skills/web.md](skills/web.md) |
