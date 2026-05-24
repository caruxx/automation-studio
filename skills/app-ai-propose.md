# app-ai-propose: Claude CLI による JSON 提案パターン

API を使わず `claude -p "<prompt>"` をターミナル経由で起動し、**単一 JSON オブジェクト**を受け取って
逐次「考案→採用」を繰り返す汎用パターン。SUNO 生成／YouTube メタ（タイトル・説明・タグ）／
ファイルリネーム（タイトル生成）など、すべてこの契約で統一している。

## 契約 (contract)

### 入力
- CLI コマンド: `claude -p "<prompt>"`（APIキー不要）
- プロンプト末尾に **Output Format — JSON ONLY** セクションを明示

### 出力
- 単一 JSON オブジェクト（コードフェンス・前後文なし）
- 各用途ごとのスキーマ:

| 用途 | JSONスキーマ |
|------|-------------|
| SUNO タイトル+スタイル | `{"title": "...", "styles": "comma,separated"}` |
| SUNO タイトル+スタイル+歌詞 | `{"title": "...", "styles": "...", "lyrics": "..."}` |
| SUNO タイトル+歌詞 | `{"title": "...", "lyrics": "..."}` |
| YouTube タイトル候補 | `{"titles": ["t1", "t2", ...]}` |
| YouTube 説明文 | `{"description": "..."}` |
| YouTube タグ | `{"tags": ["tag1", ...]}` |
| 楽曲リネーム | `{"titles": ["Title One", ...]}` |
| SUNO 一括生成 (batch) | `{"songs": [{"title":..., "styles":..., "lyrics"?:...}, ...]}` |

**SUNO スタイル記述のルール**: `styles` フィールドには以下をまとめて含める:
- ジャンル/ムード（例: `smooth jazz, lo-fi, ambient`）
- BPM（例: `120 BPM, 96 BPM`）
- 楽器・音色（例: `piano solo intro, smooth saxophone, deep sub bass, four-on-the-floor kick`）
- **曲の構造ヒント**（例: `0-5s piano only, 5-30s percussion layers in, 30s+ full arrangement, outro fade out`）

歌詞モード (`lyrics_styles` / `lyrics`) では歌詞内に Suno セクションタグ
`[Intro] / [Verse] / [Pre-Chorus] / [Chorus] / [Bridge] / [Outro]` を含める。
インストゥルメンタル寄りなら `[Intro - piano solo]` のようにブラケット内に
構造説明を書く（SUNO がこれを見てアレンジを考える）。

### パース（Python）
1. ```json ... ``` のコードフェンスがあれば剥がす
2. 先頭 `{` と末尾 `}` で挟まれた範囲を抽出
3. `json.loads()` を試す → 失敗時は末尾カンマを削除して再試行
4. スキーマ必須キーがなければ `RuntimeError`

実装リファレンス:
- [Python/suno_auto_create.py](../Python/suno_auto_create.py) — `call_claude_cli` + `_extract_json_object`
- [Python/claude_proposer.py](../Python/claude_proposer.py) — `propose_titles` / `propose_description` / `propose_tags` / `gather_context`

## 呼び出し箇所

| 場所 | 関数 | プロンプト |
|------|------|----------|
| SUNO 生成（Web） | `suno_auto_create.generate_content(provider="claude")` | `APPEND_PROMPT_JSON_*` |
| SUNO 一括生成（N 曲まとめ） | `suno_auto_create.generate_content_batch` | `APPEND_PROMPT_JSON_BATCH` |
| 動画タイトル ×5 | `claude_proposer.propose_titles` | `_TITLES_PROMPT`（v3: サムネ Read + ベンチマーク自動注入） |
| 動画説明文 | `claude_proposer.propose_description` | `_DESCRIPTION_PROMPT`（v3: サムネ+タイトル Read + ベンチマーク） |
| 動画タグ | `claude_proposer.propose_tags` | `_TAGS_PROMPT`（v3: ベンチマーク自動注入、サムネは未使用） |
| 楽曲リネーム（サムネ経由） | `app_process_tracks.propose_titles_from_thumbnail` | `claude -p "...{titles:[...]}..." --allowedTools Read` |
| 楽曲リネーム（ペルソナ経由） | `app_process_tracks.propose_titles_from_persona` | `channel_name + persona` をコンテキスト |

## サムネがない時はペルソナで提案

`app_process_tracks.py` の `process_folder()` 内で、タイトル生成は以下の優先順:

1. **サムネ画像** が見つかれば `propose_titles_from_thumbnail()` で Read ツール経由生成
2. サムネが無ければ `_load_channel_context(folder)` で
   `~/.config/{app_id}/dashboard_config.json` の `persona` を読み込み
3. `propose_titles_from_persona(cli_cmd, channel_name, persona, count)` でチャンネル世界観から提案
4. どちらも失敗 → 既存ファイル名を保持してリネームスキップ

これにより動画初期（サムネ未配置）でも「チャンネルのコンセプトに合う英語タイトル」を得られる。

## コンテキスト収集

動画フォルダから Claude に渡す情報:
- `youtube_title.txt` — 現タイトル
- `music/*.mp3` のファイル名リスト → 楽曲名
- フォルダ名末尾の `_YYMMDD` → 公開日
- `dashboard_config.json` の `persona` → チャンネル世界観

これを `claude_proposer.gather_context(folder)` が一括抽出。

### ベンチマーク分析の自動注入（v3）

`propose_titles` / `propose_description` / `propose_tags` は、`~/.config/{app_id}/competitor_analysis_cache.json` が存在すれば `analysis` 部分を読み込んでプロンプトに視聴者文脈として自動注入する（`_load_benchmark_analysis()` + `_format_benchmark_section()`）。

注入されるフィールド:
- `buzz_patterns.viewer_needs` / `title_patterns`（日本語、視聴者ニーズの理解用）
- `buzz_patterns.keywords`（英語、検索シード）
- `trend_shift.from_buzz_to_recent` / `underserved_niches`（日本語）
- `recommendations.title_tips` / `description_tips`（日本語）
- `recommendations.tag_suggestions`（英語、タグシード）

**出力は常に英語固定**（タイトル/説明/タグは英語のまま）。日本語の分析メモは「視聴者を理解するための文脈」として消費され、出力には漏れない設計。

キャッシュが無い／空の場合は persona のみで動作（後方互換）。`benchmark_analysis=` 引数で外部から明示的に渡すことも可能。

### サムネイル Vision 入力（v3）

`gather_context(folder)` は動画フォルダ直下の `vol*.jpg` / `サムネイル.jpg` / `vol*.png` を自動検出して `thumbnail` フィールドに Path で返す（`_find_thumbnail()`）。

`propose_titles(thumbnail=...)` / `propose_description(thumbnail=...)` にこの Path を渡すと、内部で:
1. プロンプトに「Read ツールで `{thumbnail}` を読み取って、視覚的シーン（時間帯・色・被写体・ムード）を特定してから出力せよ」を追加
2. `_run_claude(..., allow_read=True)` で `--allowedTools Read` を付与して Claude CLI を起動
3. タイムアウトを延長（titles=240s / description=300s）

`propose_tags` はサムネを使わない仕様（タグは検索行動ベースのキーワード抽出が主目的のため、視覚情報は寄与が小さい）。`gather_context` の `thumbnail` キーは `**_extra` で吸収して無視する。

サムネ未配置時は `thumbnail=None` → 従来挙動（楽曲名 + ペルソナ + ベンチマーク文脈のみ）。動画初期段階でも提案できる設計。

## 採用フロー（Web UI）

**動画詳細 → 詳細タブ**:
1. 「✨ AI提案 ×5」クリック → `POST /api/videos/{name}/suggest {mode:"titles", count:5}`
2. 返却された `titles[]` をクリック可能なカードで表示
3. クリック → `<input id="vdTitle">` に反映 + `PUT /api/videos/{name}/title` で即保存
4. 保存後は `refreshListsQuiet()` でコンテンツ一覧／ダッシュボードを再取得（表示反映）

説明文は「採用（上書き）/ 末尾に追加 / 破棄」の3択 UI、タグは個別追加と一括採用を用意。

## なぜ API ではなく CLI なのか

- APIキー管理不要（`claude` CLI 1 つでローカル認証が完結）
- プロバイダー切替（Gemini / ChatGPT / Claude）に混ぜられる
- ワンショット呼び出しで再現性が高い（ステートを持たない）
- 同じ契約を SUNO・リネーム・メタ提案で使い回せる

## プロンプト設計原則

1. **役割**: 最初の1-2行でペルソナを明示（例: "You are helping craft YouTube titles for a BGM channel"）
2. **コンテキスト**: 楽曲名・公開日・現タイトル・チャンネルペルソナを列挙
3. **要件**: 言語（English primary）、長さ制限、クリックベイト回避など
4. **スキーマ**: 最後に「Respond with a SINGLE JSON object, no markdown fences」を強調
5. **例示**: スキーマの `{...}` を生の形で示す（LLM が真似しやすい）

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| `claude CLI が見つかりません` | PATH 未通し | 設定画面「Claude CLI コマンド」に絶対パス（`/Users/xxx/.local/bin/claude`） |
| `JSON 抽出失敗` | LLM が余計な前文を付けた | プロンプトに `no prose before or after` を強めに追記 |
| タイムアウト | 歌詞生成が長い | `_run_claude(timeout=300)` に延長 |
