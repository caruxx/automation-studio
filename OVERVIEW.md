# Automation Studio — 導入方法と仕様

YouTube の BGM チャンネル向けに、動画づくりのほぼ全工程を自動化するツールです。
「音楽をつくる → 表紙画像をつくる → 動画編集ソフトに並べる → 書き出す → 説明文を書く → YouTube に投稿する」までを、1 つの管理画面からまとめて動かせます。

このドキュメントは前半が**どなたでも読めるやさしい解説**、後半が**開発者向けの技術仕様**です。むずかしい言葉は末尾の[用語集](#用語集)を参照してください。

> 同じ内容をブラウザで見やすくした版が `automation_studio_overview.html` にあります（GitHub 上ではソース表示になるため、クローン後に `open automation_studio_overview.html` で開いてください）。

---

## 目次

**はじめての方へ（やさしい解説）**
1. [ひとことで言うと](#1-ひとことで言うと)
2. [できること](#2-できること)
3. [導入方法（セットアップと起動）](#3-導入方法セットアップと起動)
4. [3 つの使い方（操作の入口）](#4-3-つの使い方操作の入口)
5. [AI（Claude と Codex）の役割](#5-aiclaude-と-codexの役割)
6. [動画ができるまでの流れ（11 工程）](#6-動画ができるまでの流れ11-工程)

**くわしい仕様（開発者向け）**

7. [全体のしくみ（アーキテクチャ）](#7-全体のしくみアーキテクチャ)
8. [Claude / Codex 切替回路（フォールバック）](#8-claude--codex-切替回路フォールバック)
9. [AI が動く場所の一覧](#9-ai-が動く場所の一覧)
10. [独立プログラム一覧](#10-独立プログラム一覧)
11. [skills とは何か](#11-skills-とは何か)
12. [ファイルで状態を管理（データ契約）](#12-ファイルで状態を管理データ契約)
13. [設定 / つながる外部サービス](#13-設定--つながる外部サービス)
14. [注意点・つまずきやすい所](#14-注意点つまずきやすい所)
15. [用語集](#用語集)

---

## 1. ひとことで言うと

これは「YouTube に上げる音楽動画を、なるべく自分の手を動かさずに作るための工場」のような道具です。ふだん人が時間をかけてやっていた作業を、ボタン 1 つ、あるいは話しかけるだけで、機械と AI が代わりにやってくれます。

- **たとえると「自動の動画工場」** — 材料（音楽・画像・文章）を流し込むと、ベルトコンベアのように各工程を通って、最後に YouTube 投稿まで完成品が出てきます。
- **「考える仕事」は AI 担当** — 曲名、説明文、サムネの言葉など頭を使う部分は AI（Claude／Codex）が考えます。
- **「手を動かす仕事」は機械担当** — 音楽生成サイトの操作、動画編集ソフトへの配置、YouTube 投稿はプログラムが自動で行います。

---

## 2. できること

すべて自動、または半自動です。

| できること | 内容 |
|---|---|
| 音楽を自動で作る | 音楽生成 AI「SUNO」に 1 本ぶん（例：20 曲）をまとめて作らせる。曲名・雰囲気の指定も AI が考える |
| 音楽をきれいに整える | 英語タイトルを付け直し、フェードアウト、音量そろえ（LUFS 正規化）まで自動 |
| サムネイル（表紙画像）を作る | ライバルの人気動画を参考に背景画像を生成し、文字をのせて表紙を 2 種類出力 |
| 動画編集ソフトに自動配置 | Adobe Premiere Pro を自動操作し、音楽・字幕・画像を時間どおりに並べて長尺動画を組み立て |
| 動画ファイル（MP4）を書き出す | 投稿用ファイルを自動書き出し、出来上がりも自動チェック（QA） |
| タイトル・説明文・タグを AI 提案 | タイトル候補 5 つ・説明文・タグを生成。さらに他言語へ自動翻訳も可能 |
| YouTube へ自動投稿 | 限定公開・一般公開・予約投稿に対応。投稿後は「投稿済み」の印が自動で付く |
| ライバル分析で作戦立て | 伸びている競合を調べ、「採用する点・避ける点・自分流に進化させる点」の 3 つに分けて提案 |
| 外出先のスマホから操作 | Cloudflare Tunnel 経由で外からでも同じ管理画面を開ける |
| 時間がきたら自動運転（準備中） | 「毎週この時間に新しい動画を作る」無人運転のしくみ（最終の起動スイッチは安全のため保留中） |

---

## 3. 導入方法（セットアップと起動）

### 動作要件（必要なもの）

**共通・必須**
- macOS 13 以降（Windows 対応は将来検討中）
- Python 3.10 以降
- Claude CLI（AI の第一候補・ローカル認証・API キー不要）

**Adobe Creative Cloud（動画工程に必須・有料サブスク）**

動画の配置・書き出し・サムネ合成には Adobe 製ソフトが必要で、Premiere Pro / Media Encoder / Photoshop はいずれも **Adobe Creative Cloud の有料サブスク（月額契約）が別途必要**です（本ツールに Adobe ライセンスは含まれません。Adobe との契約・支払いは各自で行ってください）。

- **Adobe Premiere Pro 2024 以降** … 音源・字幕・画像の自動配置／シーケンス組み立て
- **Adobe Media Encoder** … MP4 書き出し（Premiere とセットで導入）
- **Adobe Photoshop** … サムネ合成・2 枚出し（新版は UXP 連携／旧版は AppleScript）

**API キー・認証（UI から登録）**
- OpenAI（背景・AI サムネ画像生成）
- YouTube Data API v3 + OAuth（投稿）
- 任意：Gemini（プロンプト生成の選択肢）、Codex CLI（AI の控え・要 `codex login`）

**Adobe が無くても使える範囲**
- 楽曲生成・後処理（SUNO + ffmpeg）／メタ生成・多言語化／競合・ベンチマーク分析
- **Adobe が必須なのは「配置・書き出し・サムネ合成」の 3 工程のみ**です

### インストール

```bash
git clone https://github.com/caruxx/automation-studio.git
cd automation-studio
pipx install ".[all]"   # SUNO / Premiere 全部入り
# または最小（Web ダッシュボードのみ）:
pipx install .
```

ブラウザ自動化（SUNO 操作）を使う場合は Playwright のブラウザも導入します。

```bash
playwright install chromium
```

### 初回セットアップ

```bash
bash scripts/setup.sh   # Homebrew / Python 依存 / プリセット配置
```

### 起動

```bash
automation-studio       # サーバー起動 → http://localhost:8888
```

ブラウザで開いたら「基本設定」タブで次を設定します。

1. チャンネル名・チャンネルフォルダ
2. API キー（Gemini / OpenAI / YouTube Data API）
3. ブランド表示名（ヘッダや PWA に表示する任意の名前）

AI は Claude CLI を第一候補に使います（API キー不要・ローカル認証）。控えの Codex を使う場合は事前に `codex login` が必要です。

---

## 4. 3 つの使い方（操作の入口）

動かし方は 3 通り。どれを使っても、最終的には同じ 1 つの中枢プログラム（`app.py` / ポート 8888）が動きます。

| 入口 | 説明 | 向いている人 |
|---|---|---|
| ① 画面のボタンを押す | アプリを開き、動画ごとのボタン（音楽を作る・サムネを作る・投稿する 等）を押すだけ | いちばん分かりやすい。外出先のスマホからも可 |
| ② AI に日本語で頼む | 「vol.78 を作って」のように AI アシスタント（Claude Code）へ頼むと、必要な操作に翻訳して実行 | ボタンを探さず話しかけたい人 |
| ③ 時間がきたら自動で（準備中） | あらかじめ予定を組むと、人がいなくても作業を進める | 無人運転したい人。投稿だけは人の確認が必要な設計 |

> 「②の AI に頼む」で出てくる **Claude Code** と、「サムネの文字や曲名を考える AI」は、同じ Claude でも役割が違います。前者は**あなたの代わりに操作する秘書**、後者は**中身を考える職人**です。

### 自然言語 → 実行マッピング（②の一例）

| ユーザーの言い方 | 実行される内容 |
|---|---|
| 「vol.78 を作って」 | `curl POST /api/videos/create {"publish_date":"…"}` |
| 「楽曲を作って」 | `python3 suno_auto_create.py --workspace vol_vol78 …` |
| 「背景画像を作って」 | `app_pipeline.py <vol> --only bgimage` |
| 「サムネを作って」 | `app_pipeline.py <vol> --only psd_composite`（正規）/ `--only thumbnail`（AI 直接） |
| 「Premiere で配置して」「書き出して」 | `curl POST /api/premiere/run` ・ `/export` |
| 「タイトルを提案して」 | `curl POST /api/videos/…/suggest {"mode":"titles"}` |
| 「アップロードして」 | `curl POST /api/youtube/upload {"video_name":"…"}` |
| 「全部やって」 | `python3 app_pipeline.py <vol>` |
| 「Premiere からやり直して」 | `python3 app_pipeline.py <vol> --from premiere` |
| 「競合を分析して」 | `python3 app_competitor.py --analyze` |

詳細な運用コマンドは [AGENTS.md](AGENTS.md) を参照してください。

---

## 5. AI（Claude と Codex）の役割

このツールの「考える部分」はすべて AI に任せています。使う AI は 2 つあり、片方が使えない時にもう片方へ自動でバトンタッチするので、途中で止まりにくいのが特長です。

- **ふだんの担当：Claude** — 曲名・説明文・サムネの文字・ライバル分析など。パソコン上の `claude -p` を呼び出すので、特別な契約キーは不要。
- **控えの担当：Codex** — Claude が混雑・上限・エラーで使えない時に自動で切り替わる。使うには事前に `codex login` が必要。

かんたんに言うと「Claude が手いっぱいなら Codex が代わりに考える」。この自動切替のおかげで、夜間の無人運転でも作業が途切れにくくなっています。

---

## 6. 動画ができるまでの流れ（11 工程）

1 本の動画は次の順番で組み立てられます。各工程の「完了」は、フォルダ内に決まったファイルが出来ているかで判断します。

```
音楽生成 → 仕上げ → 背景画像 → サムネ合成 → 編集ソフト配置 → 書き出し
   → 自動チェック → 説明文作成 → 多言語化 → サムネ最終(予備) → 投稿
 (SUNO)  (名前/音量) (AI生成)  (文字のせ)  (Premiere)    (MP4)
   (QA)     (AI)     (翻訳)              (YouTube)
```

| # | 工程 | 内容 | 完了判定ファイル | 担当 | AI |
|---|---|---|---|---|---|
| 1 | suno | SUNO で楽曲生成（既定 20 曲・一括） | `music/*.mp3` ≥1 | suno_auto_create.py | 歌詞/スタイル |
| 2 | rename | 英語タイトル化＋ffmpeg 後処理 | `music/*.mp3`（処理済） | app_process_tracks.py | 命名 |
| 3 | bgimage | ベンチ参照＋コンセプトで背景生成 | `vol{N}.png/.jpg` | app_image_prompt / codex_imagegen | プロンプト+画像 |
| 4 | psd_composite | Photoshop で背景＋文字合成（2 枚） | `vol{N}.jpg` + `サムネイル.jpg` | app_photoshop.py | 文字案 |
| 5 | premiere | .prproj 自動配置→実測 SRT 生成 | `subtitles_*.srt` | app_premiere.py | — |
| 6 | export | Media Encoder で MP4 書き出し | `vol_vol*.mp4` | app_premiere.py --export | — |
| 7 | qa | 書き出し直後に自動品質チェック | QA 4 軸 pass | app_pipeline step_qa | — |
| 8 | meta | タイトル/説明文/タグを AI 生成 | `youtube_description.txt` + `tags.txt` | claude_proposer.py | メタ生成 |
| 9 | localization | メイン言語→他言語へ翻訳 | 多言語メタ | app_pipeline step_localization | 翻訳 |
| 10 | thumbnail | AI サムネ生成（PSD 失敗時の予備） | `thumbnail.png` / `vol*.jpg` | app_channel_thumbnail.py | 画像+Vision |
| 11 | upload | YouTube へ投稿（限定/公開/予約） | `youtube_upload.json` | app_youtube.py | — |

- 自動運転（使い方③）のときだけ先頭に「作戦立て（plan）」が 1 段増えて 12 工程になります。
- サムネは通常「背景画像 → 文字のせ」の順（正規フロー）で作り、うまくいかない時だけ工程 10 で AI が直接つくり直します。
- 工程の真実源は `app_pipeline.py` の `STEPS`。工程を増減する時は `STEPS / STEP_LABELS / STEP_FUNCS / RETRY_POLICY` の 4 箇所を一貫更新する規約です。

---

## 7. 全体のしくみ（アーキテクチャ）

3 つの操作入口は、すべて `app.py`（ポート 8888 の FastAPI サーバー）という**単一の中枢**に集まります。中枢は工程ごとの独立プログラムを子プロセスで呼び出し、外部サービス（SUNO・Adobe・YouTube・AI）を操作します。状態はデータベースではなく「フォルダ内のファイル」で管理します。

```
  ┌─ 操作の入口（3つ）────────────────────────────────────────────┐
  │ ① Web 管理画面        ② 日本語で AI に依頼         ③ 自動おまかせ運転 │
  │   (ブラウザ / スマホ)     (Claude Code が翻訳)         (スケジューラ)   │
  └─────────┬───────────────────┬──────────────────────┬─────────────┘
            │ HTTP / WebSocket   │ HTTP / コマンド直実行   │ 定時の合図
            ▼                    ▼                       ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║   app.py (ポート8888)  ＝ 中枢（FastAPI）   どの入口も最後はここへ   ║
  ║   app_core.py(共通部品) + routers/(機能別の窓口) + 自動運転の土台   ║
  ╚══════════╤════════════════════════════════════════════╤══════════╝
             │ 子プロセスで各工程の独立プログラムを起動       │ ファイル読み書き
             ▼                                              ▼
  ┌──────────────────────────────────────────┐   ~/.config/orzz/ (設定)
  │ 音楽生成 / 仕上げ / 背景・サムネ / Premiere /│   動画フォルダ (状態＝真実)
  │ 書き出し / 投稿 / 流れ統括 / 自動運転        │   SQLite (順番待ち・履歴台帳)
  └────┬───────────┬──────────────┬────────────┘
       ▼           ▼              ▼
   Playwright   Adobe Premiere   YouTube Data API v3
   →suno.com    + Media Encoder  （google のライブラリ経由）
       │
   ┌───▼───────────────────────────┐
   │ app_llm_runner：まず Claude →   │ ← AI への依頼は必ずここを通る
   │ ダメなら Codex に自動で切り替え   │
   └───────────────────────────────┘
```

### 技術スタック

| 役割 | 使っている技術 |
|---|---|
| サーバー本体 | FastAPI（Python 3）+ Uvicorn（自動リロード無し） |
| 画面（フロント） | 静的 HTML + 素の JavaScript（ビルド不要・`web/static/index.html` に同梱） |
| サイト自動操作 | Playwright（SUNO・Flow をブラウザごと自動操作） |
| 動画編集 | Adobe Premiere Pro + Pymiere + ExtendScript(JSX) |
| 動画書き出し | Adobe Media Encoder（JSX 経由でキュー投入） |
| サムネ合成 | Photoshop（新版は UXP のファイル連携、旧版は AppleScript） |
| 投稿 | YouTube Data API v3 + OAuth |
| AI（文章・画像・画像理解） | Claude CLI（第一候補・API キー不要）→ Codex CLI（控え）。任意で Gemini・ChatGPT・OpenAI Image・Google Flow |
| 状態の保存 | フォルダ内ファイル + SQLite（順番待ち・履歴）+ JSON（設定） |
| 通知 | Discord Webhook |
| 自動運転 | APScheduler（時刻・曜日の予約実行） |
| 外部アクセス | Cloudflare Tunnel + 認証 + PWA |

> 注意: サーバーは自動リロード無しで起動します。Python を変更したら `bash Python/start.sh` で**再起動しないと反映されません**。

---

## 8. Claude / Codex 切替回路（フォールバック）

「AI に考えさせる処理」は**必ず `app_llm_runner.py` の `run_llm()` / `run_llm_vision()` を通ります**。文章生成も画像理解（Vision）もここに集約され、全モジュール（約 26 箇所）の AI 呼び出しが共通化されています。新しく作る処理も `run_llm` 必須で、直接 `claude -p` を叩くのは禁止です。

```
  呼び出し元（音楽 / 説明文 / 背景 / 分析 / サムネ文字 / 画像採点 …）
        │  run_llm(プロンプト) / run_llm_vision(プロンプト, 画像)
        ▼
  ① まず Claude CLI を試す
     claude -p <プロンプト> [--allowedTools Read] [--add-dir …]
     成功（終了コード0かつ出力あり）→ そのまま返す
        │ 失敗（エラー / 空 / タイムアウト / コマンド無し ＝上限など）
        ▼
  ② Codex CLI に自動で切り替え
     codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
       [-i <画像> …] -o <最終メッセージ保存先> <プロンプト>
     成功 → 「Codex で成功」をログに残して返す
        │ 両方とも失敗
        ▼
  LLMError を送出（401系なら「codex login を」と案内付き）
```

- **第一候補 Claude CLI** — `claude -p "<プロンプト>"` を子プロセス起動。API キー不要でローカル認証完結。画像理解時は画像の親フォルダを `--add-dir` で許可し `--allowedTools Read` を付ける。CLI パスは `suno_config.json` の `claude_cli` で解決。
- **控え Codex CLI** — Claude が上限・エラー・タイムアウト・未検出になると自動で `codex exec` に引き継ぐ。`-o` の最終メッセージファイルを最優先で読む（無ければ標準出力）。画像は `-i` で渡す。事前に `codex login` が必要（未認証だと 401 で両方失敗・認証は失効しやすい）。
- 切替は既定で有効。環境変数 `APP_LLM_FALLBACK=0` で無効化できる（その場合 Claude 失敗で即エラー）。切替は**一方向（Claude → Codex のみ）**。

---

## 9. AI が動く場所の一覧

出力は基本的に**単一の JSON オブジェクト**で受け取り、コードフェンス除去 → `{…}` 抽出 → `json.loads()`（失敗時は末尾カンマ除去で再試行）でパースします。

| 場面 | 何を生成 | 出力の形（例） | 担当 |
|---|---|---|---|
| 音楽生成 | 曲名 / 雰囲気 / 歌詞（一括 N 曲可） | `{"songs":[{title,styles,lyrics?}]}` | suno_auto_create |
| 曲のリネーム | サムネ/ペルソナ起点の英語タイトル群 | `{"titles":[…]}` | app_process_tracks |
| 動画メタ | タイトル×N / 説明文 / タグ | `{"titles":[]}` / `{"description":}` / `{"tags":[]}` | claude_proposer |
| 多言語化 | メイン言語メタ → 他言語へ翻訳 | 各言語の title/description | step_localization |
| 背景画像 | ベンチ分析から動的プロンプト → 画像 | 画像（PNG/JPG） | app_image_prompt / codex_imagegen |
| サムネ文字 | AI 画像を見て焼き込む英字フレーズ | 英大文字フレーズ | scene_text_generator |
| サムネ採点 | 100 点満点で採点＋自動承認 | スコア / 判定 | app_thumbnail_scoring |
| ベンチ分析 | コンセプト/タイトル/サムネ/説明文 4 軸 | 各軸の構造化分析 | app_benchmark_* |
| 競合提案 | 採用/回避/進化 の 3 軸 | 提案 JSON | app_competitor |
| 作戦立て（無人時） | 今回の制作方針 | `plan.json` | app_orchestrator |

> 出力メタ（タイトル/説明/タグ）の**ソース言語はチャンネル別**。per-channel の `default_language`（＝メイン言語）でメタを生成し、`localization` 工程が他言語へ翻訳します（メイン言語自体は除外）。例：orzz=英語・SUKIMA=日本語。分析の解説文は日本語です。

---

## 10. 独立プログラム一覧

操作の入口を受ける**中枢**と、実際に作業する**各工程プログラム**に分かれます。

### 中枢

| プログラム | 行数 | 役割 |
|---|---|---|
| `app.py` | 7,921 | FastAPI 組み立て + 実行基盤。3 つの入口がすべて集約する単一頭脳。ミドルウェア/起動フック/ワーカー基盤（書き出し監視・順番待ち・スケジューラ・公開ゲート） |
| `app_core.py` | 1,432 | 共通部品。パス・設定の定数、設定ローダ、チャンネル別設定、認証ヘルパ、各種ヘルパ |
| `routers/` | — | 機能別の窓口。benchmark / images / premiere_photoshop / youtube |
| `web/static/index.html` | — | 管理画面 UI（JS 同梱・ビルド不要） |

### 流れの統括・自動運転

| プログラム | 行数 | 役割 |
|---|---|---|
| `app_pipeline.py` | 2,940 | 一括パイプライン統括。`STEPS` が工程の真実源。`--only/--from/--dry-run/--from-benchmark/--via-api` |
| `app_orchestrator.py` | 650 | 自動運転の司令塔。StageWorker/QAWorker/PlanWorker。書き出し〜サムネを自動投入、投稿は手動ゲート、連続 3 失敗でブレーカー |
| `app_render_queue.py` | 395 | Premiere/Media Encoder の「同時 1 つ」制約を吸収する SQLite 順番待ち |
| `app_run_ledger.py` | 492 | 中央の実行台帳（SQLite）。全 run 履歴を一元・永続記録 |
| `app_token_health.py` | 294 | 認証期限チェッカ。期限が近いものを Discord で事前通知 |
| `app_image_eval_loop.py` | 315 | 自己評価つき画像生成ループの核（生成→採点→弱点還流） |

### 各工程の担当

| プログラム | 行数 | 役割 |
|---|---|---|
| `suno_auto_create.py` | — | Playwright で suno.com を操作し楽曲生成。Workspace 管理・一括生成（`--batch`）・一括 DL |
| `app_process_tracks.py` | 850 | 楽曲後処理。英語リネーム + ffmpeg（無音トリム/フェード/LUFS 正規化）+ 透かし除去 + ID3 |
| `app_premiere.py` | 906 | Pymiere で .prproj を開き JSX 自動配置、実測 SRT 生成。`--export`/`--images-only` |
| `app_photoshop.py` | 1,017 | Photoshop 連携。背景＋文字で `vol{N}.jpg` + `サムネイル.jpg` を 2 枚出し |
| `app_youtube.py` | 919 | YouTube 投稿。保存ファイルから自動読込→`videos().insert()`。`--auth-only` で再認証 |
| `app_image_prompt.py` / `codex_imagegen.py` | — | 背景画像のプロンプト構築 / OpenAI Image・Codex CLI で生成（並列・参照画像対応） |
| `app_channel_thumbnail.py` | 278 | AI サムネ一括生成（PSD 合成の予備経路）+ 採点 |
| `claude_proposer.py` / `scene_text_generator.py` | — | 動画メタ提案 / サムネ焼き込み文字の生成 |

### 分析・調査

| プログラム | 行数 | 役割 |
|---|---|---|
| `app_competitor.py` | 1,324 | 競合分析の総合エントリ。`--analyze` / `--propose <vol>` |
| `app_sheets.py` | 1,040 | Google スプレッドシート取得 + パース + マッチング（API 消費ゼロ） |
| `app_benchmark_analyze.py` | 513 | ベンチ分析・提案層の本体 |
| `app_benchmark_*.py`（4 軸） | — | concept / title / thumbnail / description。`app_benchmark_common.py` が無駄な AI 消費を削減 |
| `app_channel_cache.py` | 167 | チャンネル単位のキャッシュ |
| `app_series.py` / `app_thumbnail_scoring.py` / `app_thumbnail_state.py` | — | シリーズ画像案 / Vision 100 点採点 / 各動画の状態 CRUD |

### 補助・基盤

| プログラム | 役割 |
|---|---|
| `app_llm_runner.py` | Claude→Codex フォールバック共通ランナー（全 AI 呼び出しの集約点） |
| `save_auth.py` | Flow ログインの storage_state 保存 |
| `jsx_bundle.py` | JSX バンドル補助 |
| `app_notify.sh` | Discord 通知（シェル） |
| `start.sh` / `setup.sh` | 起動（リロード無し）/ 依存導入 |

---

## 11. skills とは何か

`skills/` フォルダは、プログラム本体ではなく**AI アシスタント（使い方②の Claude Code＝あなたの代わりに操作する秘書役）のための手順書集**です。中身は人間も読める Markdown 文書で、各機能を「どのコマンドで・どの順番で・どんな注意点で」実行するかが書かれています。

- **役割：AI の取扱説明書** — 「サムネを作って」と頼むと、AI はまず `app-thumbnail.md` を読んで正しいやり方と落とし穴を理解してから作業します。だから指示がざっくりでも正しい手順で動きます。
- **位置づけ：実行はしない・案内する** — skills 自体は動くプログラムではありません。実際に動くのは `app_*.py`。skills は「どのプログラムをどう呼ぶか」を AI に教えるナレッジ層で、上記「使い方②」を支えています。

現在 24 本。主なもの: `app-workflow.md`（全体フロー）/ `app-web-dashboard.md`（API 全体像）/ `app-suno-download.md` / `app-rename-audio.md` / `app-bgimage.md` / `app-thumbnail.md` / `app-psd-composite.md` / `app-premiere.md` / `app-export.md` / `app-youtube-upload.md` / `app-competitor-spreadsheet.md` / `app-imitate-evolve.md` / `app-schedule.md` / `app-remote-access.md` ほか。全文は `skills/` 内の各 `.md` を参照。

> これはリポジトリ内の `skills/` のこと。Claude Code の個人設定 `.claude/skills`（壊れた空リンク）とは別物で、リポジトリには含めていません。

---

## 12. ファイルで状態を管理（データ契約）

データベースを持たず、動画フォルダ `{連番}_{prefix}_{YYMMDD}` の中の「決まった名前のファイルがあるか」で進捗を表します。フォルダを見れば状態が分かり、Google ドライブ同期でそのまま複数端末共有できます。

| ファイル | 意味 |
|---|---|
| `music/*.mp3` | 後処理済み楽曲（音楽＋仕上げ 完了） |
| `original_music/*.mp3` | SUNO ダウンロード直後のオリジナル |
| `vol{N}.png` / `.jpg` | 背景画像（背景生成 完了） |
| `vol{N}.jpg` + `サムネイル.jpg` | 合成済サムネ 2 枚（サムネ合成 完了） |
| `selected_images.json` | 画像選択結果（main=0-5秒 / sub[]=以降を等分配置、JSX 連動） |
| `subtitles_{N}.srt` | 実測 SRT 字幕（Premiere 配置 完了） |
| `vol_vol{N}.mp4` | 書き出し済 MP4（書き出し 完了） |
| `youtube_title/description/tags.txt` | メタ（手動 or AI 提案）。tags 未保存時は既定タグにフォールバック |
| `youtube_upload.json` | 投稿完了マーカー（video_id / url / privacy / schedule / uploaded_at） |

---

## 13. 設定 / つながる外部サービス

### 設定ファイル（`~/.config/{app_id}/`・リポジトリ外に保存）

- **チャンネル/制作**: `dashboard_config.json`（チャンネル名/フォルダ/ペルソナ/ライバル URL）, `suno_config.json`（AI プロバイダ・モデル・プロンプト・`claude_cli`/`codex_cli` パス）, `channels.json`（複数チャンネル切替）, `benchmark_config.json`, `prompts.json`, `master_prompts.json`
- **自動運転/認証/履歴**: `schedule_jobs.json`, `export_rules.json`, `export_queue.json`, `youtube_client_secret.json`, `youtube_token.json`（自動更新）, `auth_token.txt`, `discord_config.json`, `task_history.json`

> 認証情報や秘密ファイルはリポジトリに含めません（実体は repo 外）。作業ツリー内に生成される `config/`・`competitor_analysis/`・`*.bak.*` は `.gitignore` で丸ごと除外済みです。API キーは UI から個別保存し、環境変数依存はありません。

### 外部サービスと失敗時のふるまい

| 対象 | つなぎ方 | 失敗時 |
|---|---|---|
| SUNO（suno.com） | Playwright で DOM 操作（role/placeholder 優先） | DOM 変更は警告ログ・手動フォールバック可 |
| Claude CLI | 子プロセス + JSON | 失敗 → Codex CLI へ自動切替 |
| Codex CLI | `codex exec` | 未認証(401)時は `codex login` 案内付きでエラー |
| Premiere / Media Encoder | Pymiere + JSX | 未起動を検知・プリセット不在はスキップ |
| YouTube Data API v3 | google-api-python-client + OAuth | token 失効は自動更新 |
| Discord Webhook | HTTP POST | 未設定時はボタン無効 |

---

## 14. 注意点・つまずきやすい所

### サーバー / 実行
- uvicorn は自動リロード無し → 変更後は `bash start.sh` で再起動
- AI 工程（meta / localization）を `--via-api` 実行すると 10 秒タイムアウトで必ず失敗 → CLI 直で実行
- premiere / export の「完了」ログは早期誤判定あり → MP4 サイズ安定 + ffprobe で裏取り
- JSX は JSX Launcher 拡張経由のみ（AppleScript 不可）

### SUNO（音楽生成）
- Bot 判定対策で `APP_KEEP_BROWSER=1` + interval 90 を既定に
- 生成前に prompt 本文をユーザーに見せて合意を取る（ジャンル違い事故の防止）
- 複数 vol は workspace を vol 別にして混在ダウンロードを防ぐ
- 生成リクエスト送信 ≠ レンダリング完了（DL 前に待機 / cache miss は再試行）

### Premiere / Media Encoder
- batch.xml の古いジョブで別 vol を誤書き出し（最重要）
- AME 強制終了で Premiere のモデルが壊れるリスク
- Premiere 再起動後は AME も再起動しないと描画が始まらない
- PS 2026 は字幕トラックを削除できず積み上がる

### AI / Codex / 共有ドライブ
- Codex は `codex login` 必須・認証が失効しやすい。切替は Claude → Codex の一方向
- YouTube quota はローカル追跡が保守的。実 API は深夜 0 時 PT にリセット済みのことあり
- Google ドライブ上は Bash 出力が壊れることがある → 検証は /tmp 経由、git はパス限定
- 認証の有効期限切れを検出したら再ログインが必要

---

## 用語集

| 用語 | 意味 |
|---|---|
| API | プログラム同士が情報をやり取りする窓口・約束ごと |
| CLI | コマンド入力でプログラムを動かす方式。ここでは `claude -p …` のように AI を文字コマンドで呼ぶこと |
| 子プロセス / subprocess | あるプログラムが別の小さなプログラムを呼び出して実行すること |
| フォールバック | 本命がダメな時に控えへ自動で切り替えること（Claude → Codex） |
| パイプライン | 作業を決まった順番でつなげて一気に流す仕組み（11 工程の流れ作業） |
| ベンチマーク | 比較・目標にする基準。ここでは参考にするライバルチャンネルやその分析 |
| サムネイル | 動画の表紙画像。クリックされるかを大きく左右する |
| メタ（メタデータ） | 動画のタイトル・説明文・タグなど動画に付ける情報 |
| OAuth | パスワードを直接渡さずにサービス連携する認証方式（YouTube 投稿で利用） |
| SQLite | 1 ファイルで動く軽量データベース（順番待ちと履歴台帳に使用） |
| スケジューラ | 決めた時刻・曜日に自動で処理を起動するしくみ（APScheduler） |
| JSX | Adobe 製品を自動操作するスクリプト（Premiere の自動配置で使用） |

---

## 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| [README.md](README.md) | 入口・クイックスタート |
| [SPEC.md](SPEC.md) | 全体仕様（API 一覧・データ契約・アーキテクチャ） |
| [AGENTS.md](AGENTS.md) | 運用コマンド・自然言語マッピング・エラーリカバリ |
| `automation_studio_overview.html` | 本書のブラウザ閲覧版（クローン後 `open` で表示） |
| `skills/` | 機能別の手順書（AI アシスタント用ナレッジ） |

最終更新: 2026-06-08
