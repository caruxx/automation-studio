# AI ルーティング & マルチチャンネル並行運用 計画書

作成: 2026-07-03 / 計画: Claude Fable 5 / 実装: Codex 5.5

## 目的

1. **確実な動線**: Claude / Codex どちらのAIが指示を受けても、迷わず正しい Python スクリプト・API に到達する単一エントリポイントを作る。
2. **時間短縮**: 複数チャンネル運用時に、並行可能な工程(rename / bgimage / meta / localization など)をAIエージェントが安全に並行実行できる基盤を作る。

## 背景(現状の問題)

- vol番号 → `video_name`(フォルダ名 `78_vol_260420` 形式)の解決を各AIがアドホックに実装している。`/api/videos` を叩いて `num` でフィルタする手順を知らないと失敗する。
- チャンネル切替(`PUT /api/channels/active/{id}`)後、前チャンネルの `video_name` を使い回す事故が起こりうる。ガードが無い。
- `--via-api` は10秒タイムアウトのため `meta` / `localization` などLLM長時間ステップは必ず失敗する。この知識がドキュメントに散在し、AIが踏む罠になっている。
- `AGENTS.md` の自然言語→コマンド対応表は人間可読だが機械可読でなく、同義語(「サムネ」「サムネイル」)の解決もAI任せ。
- 工程ごとの並行可否(SUNOブラウザ単一 / Premiere・AME単一 / ffmpeg・LLMは並行可)がコード化されておらず、安全な並列化ができない。

## Phase A: ルーティング層(今回実装)

### A-1. `Python/routes.json` — 機械可読ルーティング表(単一の真実)

各 intent(操作意図)を1エントリとして定義:

```json
{
  "version": 1,
  "intents": {
    "bgimage": {
      "aliases": ["背景画像", "背景", "bg"],
      "description": "背景画像生成(ベンチマーク参照+チャンネルコンセプト)",
      "cli": {"cmd": ["python3", "app_pipeline.py", "{vol}", "--only", "bgimage"]},
      "api": {"method": "POST", "path": "/api/bgimage/run", "body": {"video_name": "{video_name}"}},
      "prefer": "cli",
      "via_api_safe": true,
      "requires_vol": true,
      "parallelism": {"scope": "global", "max_parallel": 2}
    },
    "meta": {
      "aliases": ["メタ", "タイトル生成", "説明文", "タグ"],
      "cli": {"cmd": ["python3", "app_pipeline.py", "{vol}", "--only", "meta"]},
      "prefer": "cli",
      "via_api_safe": false,
      "via_api_unsafe_reason": "LLM長時間ステップ。--via-api の10秒timeoutで必ず失敗する。CLI直実行のみ。",
      "requires_vol": true,
      "parallelism": {"scope": "global", "max_parallel": 3}
    }
  }
}
```

収録する intent(既存 AGENTS.md の対応表を全て移植すること):
`create` / `suno` / `suno-download` / `rename` / `bgimage` / `psd` / `premiere` / `export` / `qa` / `meta` / `localization` / `thumbnail` / `upload` / `pipeline`(全工程) / `analyze`(競合分析) / `status` / `channels` / `resolve`

parallelism の初期値(調査結果に基づく):

| intent | scope | max_parallel | 理由 |
|---|---|---|---|
| suno | per-channel | 1 | Playwrightブラウザ単一 |
| rename | global | 4 | ffmpeg CPU-bound |
| bgimage | global | 2 | 画像APIレート |
| psd | per-machine | 1 | Photoshop単一 |
| premiere / export | per-machine | 1 | Premiere/AME単一(render_queue管理) |
| qa | global | 4 | ffprobeのみ |
| meta / localization | global | 3 | LLMレート |
| thumbnail | global | 2 | 画像API |
| upload | per-channel | 1 | quota公平性 |

### A-2. `Python/studio.py` — 単一ディスパッチャCLI

AIが覚える入口はこれ1つ。routes.json を読み、解決・検証・実行する。

```bash
# 一覧と説明(AIの自己発見用)
python3 studio.py --list
python3 studio.py --explain meta

# vol番号 → video_name 解決(サーバ稼働中は /api/videos、停止中はフォルダ走査でフォールバック)
python3 studio.py resolve 78
python3 studio.py resolve 78 --channel sukima

# 実行(dry-run で解決結果のコマンドを表示するだけ)
python3 studio.py bgimage --vol 78 --dry-run
python3 studio.py bgimage --vol 78
python3 studio.py pipeline --vol 78 --from premiere
python3 studio.py meta --vol 78          # via_api_safe=false は自動でCLI直実行
```

必須挙動:
1. **vol解決**: `--vol N` を受けたら video_name に解決。解決不能なら候補一覧を出して exit 2。
2. **チャンネルガード**: 実行前にアクティブチャンネルを取得して表示。`--channel` 指定があり不一致なら実行せず exit 3(`--switch` 明示時のみ切替)。解決した video_name がアクティブチャンネルのフォルダ配下に無ければ拒否。
3. **via_api ガード**: `via_api_safe=false` の intent は `--via-api` 指定があっても拒否して理由を表示。
4. **exit code 透過**: 既存の 75/76/77/78 規約をそのまま透過する。
5. **`--dry-run`**: 解決後の実コマンド(またはcurl相当)を表示して終了。副作用ゼロ。
6. 標準出力の末尾に1行JSON(`{"ok": true, "intent": ..., "video_name": ..., "cmd": [...]}`)を出し、AIがパースできるようにする。

### A-3. FastAPI 追加エンドポイント(app.py)

- `GET /api/resolve-vol/{num}?channel_id=` → `{video_name, channel_id, folder, exists}`(studio.py と同一ロジックを共有関数化して使う)
- `GET /api/routes` → routes.json をそのまま返す(ダッシュボード・AI用)

### A-4. ドキュメント更新(両AIの動線)

- `_claude/AGENTS.md`: 冒頭に「**正規の入口は `python3 Python/studio.py`**。個別コマンドは studio.py --dry-run で確認してから使う」を追記。既存の対応表は残すが「routes.json が真実、この表は概要」と明記。
- `_claude/CLAUDE.md`: 同様の一文+ via_api_safe ルールへの参照を追記。
- `DEV/.codex/agents/youtube-automation-studio.md`: Operating rules に studio.py 経由の原則を追記。
- 新規 `_claude/skills/app-routing.md`: studio.py の使い方・exit code・チャンネルガードの1ページ手順書。

## Phase B: マルチチャンネル並行運用(承認済み: 2026-07-03)

**ユーザー決定**: 完全自動(アップロードまで無人)。ただし**投稿は予約投稿が既定**。

1. `app_orchestrator.py` の StageWorker に channel_id コンテキストを追加し、routes.json の parallelism を参照して can_run を判定。APSchedulerに登録(既存 AGENTS_WORKPLAN Phase 2 の qa-worker から着手し、全ドメインワーカーまで)。
2. **autopilot スイッチ**: チャンネル単位の ON/OFF(`.app_channel_config.json` または channels.json 側)。**デフォルト OFF で出荷**し、ユーザーがチャンネルごとに有効化する。API: `GET /api/workers/status`(各ワーカーの現在vol/stage)、`POST /api/workers/autopilot`(チャンネル別 enable/disable)。
3. **完全自動の投稿規則**: upload は qa ステージ成功が前提条件。投稿は privacy=private + publishAt=フォルダ名の publish_date(既存 publish_mode / schedule-publish 機構を利用)を既定とし、チャンネル設定で上書き可能。publish_date が過去日の場合は自動投稿せず通知して停止。
4. OAuth トークンガード: upload 実行時に使用トークン(per-channel / global)をログ出力。**autopilot 経由の upload は per-channel トークン必須**(無ければ通知して該当volを停止。global fallback は手動実行時のみ許可)。
5. render_queue にチャンネルIDを記録し、キュー表示で「どのチャンネルの何が詰まっているか」を可視化。
6. Claude側 `.claude/agents/`(7ドメイン)/ Codex側 `.codex/agents/` の双方から、並行作業時は「1エージェント=1チャンネル or 1ドメイン」で書き込み対象を分ける運用ルールを skills/app-routing.md に明記。

## Phase C: 新チャンネル立ち上げ動線(オンボーディング)

**背景**: UI設定画面が複雑で新規チャンネル作成に抵抗が大きい。「チャンネル作成→ベンチマーク先指定→直近サムネ・伸び要素の抽出分析→自分の動画への落とし込み→再生数で検証」を**もれなく**実行したい。

### C-1. チェックリスト駆動のオンボーディング状態

チャンネルごとに `onboarding.json`(チャンネルフォルダ直下 or config)で工程を管理:

```json
{
  "channel_id": "...",
  "steps": {
    "register":        {"done": false, "desc": "チャンネル登録(registry + フォルダ + config雛形)"},
    "oauth":           {"done": false, "desc": "per-channel YouTube OAuth トークン取得"},
    "benchmark_set":   {"done": false, "desc": "ベンチマーク先チャンネルの登録(URL/ID複数)"},
    "benchmark_fetch": {"done": false, "desc": "直近動画・サムネ・統計の取得"},
    "analyze":         {"done": false, "desc": "伸び要素抽出(サムネ/タイトル/コンセプト分析)"},
    "concept_apply":   {"done": false, "desc": "自チャンネルconfigへの落とし込み(参照画像dir, sunoスタイル, scene_text等)"},
    "first_vol":       {"done": false, "desc": "vol.1 フォルダ作成+plan生成"},
    "verify_loop":     {"done": false, "desc": "投稿後の再生数トラッキング+ベンチ比較の定期ジョブ登録"}
  }
}
```

### C-2. 実行動線

- studio.py に intent `channel-onboard` を追加: `python3 studio.py channel-onboard --channel <id> [--step <name>]`。未完了ステップを順に案内・実行し、対話が必要な箇所(OAuth、ベンチ先URL入力)は明示指示を出して停止。`--status` で残タスク一覧。
- API: `GET /api/channels/{id}/onboarding`(チェックリスト状態)、`POST /api/channels/{id}/onboarding/{step}`(個別ステップ実行)。
- Web UI: 設定画面の複雑さを回避するため、新規チャンネル作成時はウィザード形式(ステップバイステップ、既定値プリセット)で onboarding API を叩く1画面を追加。既存の詳細設定画面は「上級者向け」として残す。
- 分析は既存資産を束ねる: app_competitor.py / app_benchmark_concept / app_benchmark_thumbnail / app_benchmark_title / app_channel_cache(per-channel隔離)。新規分析エンジンは書かない。
- 検証ループ: 投稿済み動画の再生数・CTR系(取得可能な範囲)を定期取得し、ベンチマーク先の同期間動画と比較したレポートを生成(APScheduler 定期ジョブ、Discord通知は既存 app-notify 経由)。

### 実装順序

Phase B → 検証 → Phase C(いずれも app.py を触るため直列)。

## Phase D: 友人向け配布パッケージ化(承認済み: 2026-07-03)

**ユーザー決定**: システム名は「Automation Studio」。チャンネル設定は配布先ごとに完全独立。対象環境は Mac + Premiere/Photoshop あり。

### D-1. ブランド統一

- 「orzz ダッシュボード」「orzz dashboard」等のブランド表記を「Automation Studio」に置換(UI・README・ドキュメント・通知文言)。
- **チャンネル名としての「orzz.」参照は置換しない**(例示・プレースホルダは配布向けに一般化: 「orzz_vol74」→「mych_vol1」等、UIに出るものを優先)。
- `_app_config.py` の app_id 既定値・旧パス互換は変更しない(既存環境の移行を壊さない)。

### D-2. クリーンパッケージ生成 + 初回セットアップ

- `package.sh`(または make target): 配布用 zip を生成。
  - **含める**: Python/、web/、skills/、routes.json、テンプレート、README(配布用)、VERSION
  - **除外(必須)**: config/discord_config.json(webhook URL平文!)、config/channels.json*、competitor_analysis_cache.json、benchmark/ キャッシュ、.youtube_token.json、client_secret*、vol実データ、.claude/.codex の個人メモ
  - 除外リストは `package_exclude.txt` として明文化し、package.sh がビルド後に「個人データ混入スキャン」(webhook URL・token・実チャンネルIDのgrep)を実行して検出時は失敗させる。
- 個人設定はテンプレート化: `config/*.template.json` を同梱し、初回起動時に実ファイルへコピー。
- 初回セットアップ: config 不在を検知したら Web UI がセットアップ画面(システム名入力→Discord webhook(任意)→新規チャンネルかんたん作成ウィザードへ誘導)。CLI は `studio.py setup`。配布先ごとに独立した config/channels.json が生成される(Phase Cのオンボーディングに接続)。

### D-3. アップデート機能

- `VERSION` ファイル + `GET /api/version`。
- アップデータ: `studio.py update` と Web UI の「アップデート確認」ボタン。
  - 取得元は `config/update_config.json` で切替: `{"method":"zip_url","source":"<manifest URL>"}` または `{"method":"git","source":"<repo>"}`。
  - 手動フォールバック: `studio.py update --from-zip <path>`。
  - 更新フロー: バージョン比較 → コード領域のみ上書き(config/・ユーザーデータは触らない)→ 依存更新 → `start.sh` 再起動案内。更新前に現行コードを `backup/<version>/` に退避しロールバック可能に。

### D-4. かんたん設定UI

- 設定を「かんたん」「詳細」の2層に分離。かんたん側はチャンネル名・投稿スケジュール・SUNOスタイル・ベンチマーク先・autopilot ON/OFF のみ。既存詳細画面は「詳細設定」として温存。

### D-5. Web・AI からの操作性(現状で充足、将来項目)

- Web: localhost:8888(配布先も同様)。外部からのリモートアクセスは skills/app-remote-access.md(Cloudflare Tunnel + 認証 + PWA)を将来適用。
- AI: studio.py + routes.json が配布物に含まれるため、配布先でも Claude/Codex から同じ動線で操作可能。

### 実装順序

D-1+D-2(ブランド+パッケージ) → 検証 → D-3+D-4(アップデータ+かんたん設定UI) → 検証 → 実パッケージ生成して混入スキャン確認。

## Phase E: 運用安定化 + 設定共通化 + UI再編(承認済み: 2026-07-03)

**ユーザー決定**: UIは段階的改善(本格リファクタはしない)。更新後autopilot一時停止ガード導入。launchd常駐化導入。

### E-1. 更新×自動運転ガード

- 起動時に「前回起動時のバージョン」(`~/.config/{app_id}/last_boot_version`)と現VERSIONを比較。変わっていたら全チャンネルの autopilot を一時停止状態(`autopilot_suspended_by_update: true`)にし、Discord通知+UIバナー「更新完了。自動運転を再開してください」を表示。再開はUIボタン/API/`studio.py autopilot --resume-all` のいずれか。元からOFFのチャンネルには影響なし。

### E-3. 設定の共通化(AI指示→設定ファイル保存)

- `Python/settings_catalog.json` 新設: 全設定キーのカタログ。各キーに `label_ja`(日本語名), `description_ja`(何に効くか), `tier`(easy/advanced), `scope`(global/channel), `storage`(どのファイルのどのキーか), `type`/`choices`/`validation`。
- `studio.py config get <key>` / `config set <key> <value> [--channel <id>]` / `config search <語>`: カタログを参照して正しいファイルに検証付きで保存。routes.json に intent 追加。
- 監査ログ `~/.config/{app_id}/config_audit.jsonl`: 変更のたび {when, actor(ai/human/ui), key, old, new} を追記。UI設定保存経路にも同じ記録を仕込む。
- `GET /api/settings-catalog`。AIへの動線: skills/app-routing.md に「ユーザーの好み指示(文体・スタイル等)は config set で永続化する」を明記。プロンプト系は既存 config/prompts.json のオーバーライドに保存。

### E-4. 安定運用

- launchd 常駐化: `setup_launchd.sh`(LaunchAgent plist 生成・ロード。KeepAlive=true で自動復旧、RunAtLoad=true)。アンインストール手順も同梱。配布パッケージにも同梱し初回セットアップから案内。
- `GET /api/health`: version・uptime・scheduler稼働・render_queue worker・ディスク空きの軽量ヘルスチェック。
- ログローテーション: サーバーログを日付ローテ+7世代保持(start.sh / launchd 両経路)。
- Google Drive 同期途中ファイル対策: アップロード・QA前のファイル安定チェック(サイズ2回一致)を共通関数化し、未適用の工程にも適用。

### E-2. UI/メニュー再編(段階的)

- サイドバーナビ: ①ダッシュボード ②動画(vol一覧/詳細) ③チャンネル ④自動運転(workers/autopilot/キュー) ⑤分析(ベンチマーク/競合) ⑥設定 ⑦アップデート。既存機能は削除せず配置換え。
- ダッシュボード(ホーム)は状態ファースト: 進行中の工程、次の予約投稿、直近エラー、render_queue、autopilot状態、更新通知バナー。
- 設定画面は settings_catalog.json から自動生成(easy=かんたん設定に表示、advanced=詳細設定に折りたたみ)。検索ボックス付き。既存の手書き設定UIは段階的にカタログ生成へ寄せる(今回は easy 層と主要 advanced のみでよい)。
- HTMLの完全分割はしない(段階的)。ただし新規追加分のJSは web/static/js/ に外部ファイル化してよい。

### 実装順序

E-1+E-3+E-4(バックエンド) → 検証 → E-2(UI) → 検証 → package.sh 再ビルド確認。

## Phase F: UX改善4点(ユーザー指摘: 2026-07-04)

### F-1. シーンテキスト設定の平易化

- カード全体を「専門用語なし」で書き直す。構成:
  1. トグル「サムネに文字を入れる」(OFFなら以下を非表示)
  2. 主役ボタン「ベンチマークからおまかせ提案」(タイトル由来/Vision由来は1つのボタン+選択に統合)
  3. フィールドは平易なラベルに: 「文字の雰囲気」(旧トーン)、「参考にする言い回し(1行に1つ)」(旧語感の参考フレーズ)、「使わない言葉(1行に1つ)」(旧禁止フレーズ)
  4. 例文は説明文でなく placeholder に移動。「構文ヒント」は詳細折りたたみへ
  5. 冒頭に1行だけ: 「サムネ画像に焼き込む英語フレーズのルールです。空欄はおまかせで問題ありません。」
- 機能・保存先・API は変更しない(文言とレイアウトのみ)。

### F-2. SUNO設定から Gemini を削除

- UIのSUNO provider選択から gemini を削除。JSの既定値 'gemini' フォールバックは 'claude' に変更。
- gemini モデルリスト・「Gemini キー取得」ボタンは SUNO 文脈から撤去(他機能で gemini を使う箇所があれば残す。要調査)。
- バックエンド(suno_auto_create.py の --provider)は後方互換のため受け付けは残してよい。
- 保存済み設定が provider=gemini のチャンネルは、次回保存時に claude に置き換わる誘導(UI上で警告表示)。

### F-3. アップデートの全自動化

- APScheduler 日次ジョブ: update_config の source 設定済みかつ update_available なら自動適用。
- 適用後: E-1ガード(autopilot自動一時停止)発動 → launchd 常駐時は --sync 相当(ミラー再同期+kickstart)まで自動実行 → Discord に「vX.Y.Z へ自動更新しました。自動運転は一時停止中です」通知。
- 非常駐時は適用+「再起動してください」通知のみ(従来どおり)。
- 設定カタログに `update.auto_apply`(既定 true)を追加し、かんたん設定でOFFにできる。失敗時はロールバックして通知。

### F-4. ベンチマーク分析の簡素化

- 分析画面の既定ビューを3ステップに再構成:
  1. 「①ライバルを登録」(現在の登録数と追加UI)
  2. 「②一括分析」(既存 runBenchmarkBatchAnalysis の1ボタンのみ。進捗表示付き)
  3. 「③結果と提案」(伸びている要素の要約+自チャンネルへの提案+「この結果で制作を始める」ボタン)
- 深掘り系(プロファイル/融合、追跡、制作分析のみ、いま収集、未分析サムネ分析、候補を絞る等)は「詳細ツール」折りたたみへ移動。機能削除はしない。
- Phase C オンボーディングの benchmark_set / analyze ステップからこの3ステップUIに誘導。

## Phase G: 設定・自動運転のUX統合(ユーザー指摘: 2026-07-04)

### G-1. 設定ページの統合とチャンネル一括適用

- 「基本設定(クイックセットアップ)」「かんたん設定」「設定カタログ」が並存してバッティングしている状態を**1つの設定ページに統合**する。構成: 上=クイックセットアップ(残す)、中=チャンネル設定(カタログ生成)、下=詳細設定(折りたたみ)。同じ設定キーが2箇所以上に出ないよう重複排除(真実源はsettings_catalog)。
- チャンネルスコープ設定は**コンパクトな1行レイアウト**(ラベル+入力+保存)にし、ページ上部に「対象チャンネル」セレクタを1つだけ置く(設定ごとの「対象チャンネル: xxx」表記は削除)。
- 保存時に「**全チャンネルへ一括適用**」チェックを提供(ONで全チャンネルの .app_channel_config.json に同値を保存、監査ログにも channel=all で記録)。API: 既存 settings-catalog/value 系に channel_id="all" 対応を追加。

### G-2. 投稿設定のJST統合

- `channel.publish_time_jst`(既定 "12:00")を新設。アップロードは非公開+publishAt=フォルダ日付+この時刻(JST)で統一。
- UIの「投稿方式(限定公開/即時/遅延)」+「公開待機時間」は撤去し、**「公開時刻(日本時間)」1項目**に統合。方式は内部的に「予約公開」に一本化(即時公開したい特殊ケースは詳細設定に「即時公開する」トグルとして残す)。publish_mode / publish_delay_hours は後方互換で内部保持。
- autopilot経由アップロード(Phase B)もこの時刻を使用するよう統一。

### G-3. 自動運転ページの簡素化

- 既定ビュー: チャンネルごとに1行「チャンネル名 / 自動運転トグル / 状態バッジ(稼働中・停止中・更新後一時停止) / 今できる工程数」。冒頭に1行だけ: 「ONにすると、素材づくり→動画づくり→検査→予約投稿(公開時刻に自動公開)までを自動で進めます。」
- 「自走運用」等の分かりにくい表現は「自動運転」に統一。候補一覧・キュー・優先度・ブレーカー等は「詳細」折りたたみへ。

### G-4. サイドバーから「チャンネル」を削除

- ナビは6項目に(ダッシュボード/動画/自動運転/分析/設定/アップデート)。チャンネル管理と「新規チャンネル(かんたん作成)」は設定ページ内へ移設。ヘッダーのチャンネル切替は現状維持。

### G-5. ダッシュボードの「ベンチマークから制作」カード撤去

- ダッシュボードから撤去し、分析ページ「③結果と提案」の「この結果で制作を始める」に一本化。「自動承認モード」という表現は廃止し、実行時の説明は「作成後、自動運転がONのチャンネルでは予約投稿まで自動で進みます」に変更。機能自体(API)は削除しない。

## Phase H: 収益化・継続運営・学習ループ(承認済み: 2026-07-04)

コンセプト: 誰でも使いやすく、細かな配慮。目的: 収益化 / 安定継続 / 日々の改善 / ベンチ分析。

### H-B(先行): 運営基盤4点

1. **毎朝のDiscordダイジェスト**: APScheduler日次ジョブ(既定 08:00 JST、カタログ `digest.time_jst` / `digest.enabled` で変更可)。内容: 昨日の投稿結果(ledger) / 今日の予約投稿 / 未解決エラー / トークン・ログイン期限予兆(app_token_health活用、YouTube OAuth・SUNOセッション) / ディスク残量 / 素材在庫警告(下記2)。1通に集約、チャンネル横断。
2. **素材在庫(ストック日数)**: チャンネルごとに「予約済み+完成済み未投稿のvolが何日分あるか」を算出(publish_date基準)。ダッシュボードのチャンネル行とダイジェストに表示。`stock.warn_days`(既定7)を切ったら警告。算出ロジックは共有関数化(/api/stock)。
3. **エラーの「次にやること」化**: sentinel exit(75/76/77/78)と頻出エラーを平易な日本語+アクションに変換する共通関数(error_humanizer)。例: 75→「SUNOのログインが切れました → ダッシュボードから再ログイン」。Discord通知・UIトースト・ダイジェストの3経路で使用。既存の技術ログは詳細として温存。
4. **設定の自動バックアップ**: 日次で config/ と全チャンネルの .app_channel_config.json を Drive上 `config_backups/<日付>/` に世代保存(14世代)。`studio.py config-backup --restore <日付>` とUI(設定→詳細)から復元。監査ログに記録。

### H-A(後続): 分析・学習3点

5. **収益化達成トラッカー**: チャンネルごとに YPP 条件(登録1,000人 / 総再生4,000時間)への進捗+現ペースでの達成予測日。登録者数は Data API(公開統計)で即時対応。総再生時間は YouTube Analytics API(`yt-analytics.readonly` スコープ追加)が必要 — スコープ未許可のチャンネルは「再認証で有効化」表示のグレースフルデグレード。達成済みチャンネルは推定収益表示に切替(Analytics API)。ダッシュボードカード+ダイジェスト掲載。
6. **投稿48時間レビュー(学習ループ)**: 投稿48h後の初速(再生数は公開統計、CTR/平均視聴時間はAnalytics任意)をチャンネル移動平均と比較→ダイジェスト/Discordに「平均比±%」レポート。勝ち要素(サムネ文字・タイトル型)を `learned_patterns.json`(チャンネル別)に蓄積し、meta/thumbnail 生成プロンプトが参照する。自動書き戻しは監査ログ記録+上限件数で暴走防止。
7. **ライバル新作アラート**: 既存の追跡(新着検知・48h初速)を拡張し、ベンチ先の新作が初速で伸びたら「何が効いたか」1行AI分析付きでダイジェスト/Discordへ。

### 実装順序・注意

- H-B → 検証 → H-A → 検証 → package.sh 再ビルド。
- Analytics スコープ追加は既存トークンの再認証を要するため、H-A は「スコープ無しでも壊れない」を必須要件とする。
- 新規ジョブは全て `digest.enabled` 等のカタログスイッチでOFF可能にし、既定は digest=ON / 学習書き戻し=ON / アラート=ON。quota消費は公開統計中心で最小化。

## Phase I: クォータ最新化 + API監査(コンプライアンス)対応(承認済み: 2026-07-04)

背景(公式確認済み): videos.insert のコストが 2025-12-04 に旧高コスト→**約100ユニット**に変更。2026-06-03 に `videos.batchGetStats`(1ユニット、専用の既定1万ユニット/日枠)が追加。クォータ拡張申請には YouTube API Services の監査(開発者ポリシー準拠)が必要。

### I-1. クォータ最新化+使用量メーター

- コード・ドキュメント内の旧高コスト前提を全て 100 に更新(grep で洗い出し)。quota_exhausted(exit 77)の待機ロジックの前提も見直し。
- 統計取得(収益化トラッカー/48hレビュー/ライバル追跡)を `videos.batchGetStats` に移行(1ユニット・専用枠。フォールバックとして videos.list を維持)。
- **クォータメーター**: API呼び出しごとに {method, cost, when, channel} をローカル記録(SQLite or jsonl)。`GET /api/quota` で本日消費/上限・メソッド別内訳。ダッシュボードカード+ダイジェスト1行。監査申請時の使用実績エビデンスを兼ねる。

### I-2. コンプライアンス改修(監査を通す)

1. **プライバシーポリシーページ**: `/privacy`(日英)。必須7項目: 所在地明記 / YouTube API使用の開示 / Googleプライバシーポリシーへのリンク / 収集・保存・共有データの説明 / 第三者広告なしの明記 / デバイス情報・Cookieの扱い / Googleセキュリティ設定ページ(https://security.google.com/settings)へのリンク。OAuth同意画面用に公開URLが必要 → 会社HP(caruvistar.jp)掲載用の静的HTMLも同時生成。
2. **30日データ保持ルール**: API由来の保存データ(統計スナップショット、ベンチキャッシュ、追跡データ、learned_patterns の元データ)に取得日を必ず付与し、日次ジョブで①30日超の非統計データは削除 or 再取得、②統計データは「トークン有効性の30日ごと確認」を記録して保持継続。処理内容を `data_retention_log.jsonl` に記録(監査エビデンス)。
3. **連携解除フロー**: チャンネル連携解除UI/CLIで、トークンをプログラム失効(revoke)+該当チャンネルのAPI由来データを7日以内(実装は即時)削除。監査ログ記録。
4. **YouTube帰属表示**: API由来データを表示するUI(ベンチマーク、統計、追跡)に「データ提供: YouTube」表記とYouTubeへのリンクを付与。サムネイル等の出典明示。
5. **スコープ最小化の文書化**: 使用スコープ一覧と用途を docs/API_COMPLIANCE.md に整理(監査回答の素材)。

### I-3. クォータ拡張申請ドラフト(Fable 5作成)

- (I-3の内容は変更なし。下記 Phase J を追加)

## Phase J: Data API リサーチ最大活用 — TTP戦略(計画: 2026-07-04)

**戦略**: search.list(100units)は「新ジャンル発見」だけに絞り、追跡・分解・深掘りは1unit系(videos/channels/playlistItems/commentThreads)とbatchGetStats専用枠で回す。全機能はPhase Iのガバナンス(30日保持・帰属表示・クォータメーター)の上に載せる。

### J-2. TTP分析エンジン(最優先・追加クォータほぼゼロ)

- 既存の追跡ライバル+指定チャンネルの「勝ちフォーマット」を自動分解: タイトル構文(型のパターン化)、サムネ要素(既存Vision分析)、動画尺、投稿頻度・曜日・時刻、シリーズ構造(Vol.連番等)、タグ、公開後の伸びカーブ(batchGetStats)。
- 出力: **TTPプロファイル**(JSON+人間可読レポート)。「このジャンルで勝つための仕様書」= 尺◯時間 / 週◯本 / タイトル型『◯◯ | Vol.N | ◯◯』/ サムネ=都市夜景+大文字2語 など。
- 動線: Phase C オンボーディングの concept_apply がTTPプロファイルを直接取り込めるようにする(**新チャンネル立ち上げ=プロファイル選択から始まる**)。既存 imitate-evolve(adopt/avoid/evolve)の3軸判定を統合し「完コピ回避」(禁止フレーズ・差別化ポイント)も同時生成。
- データ源: playlistItems(uploads)=1u + videos.list=1u + batchGetStats(専用枠)。1チャンネル分解 ≒ 5〜10units。

### J-3. 需要ギャップ分析 — コメントマイニング(1unit系・高付加価値)

- ジャンル上位動画の commentThreads.list(1u)を取得し、LLMで「視聴者の要望・不満・繰り返し出る利用シーン(作業用/睡眠用/勉強用等)」を抽出。
- 出力: チャンネル別「需要メモ」→ シリーズ提案(app_series)と learned_patterns に接続。「コメントで◯◯が欲しいと言われているのに誰もやっていない」= 次のvolのコンセプト。
- 保持は30日ルール適用(要約=自社創作物は保持可、元コメントデータは期限管理)。

### J-1. ジャンルレーダー(search予算制)

- videos.list chart=mostPopular(地域×音楽カテゴリ、1u)を日次スナップショット+週次で search.list を**予算制**(既定10回=1,000u/週、カタログ `research.search_budget_week`)で実行し、BGM隣接ジャンルの新興チャンネル・急伸フォーマットを発見。
- 成長率ランキング(登録者・初速の週次差分)→ 週次ダイジェストに「参入候補ジャンル TOP5」(TTPプロファイル生成ボタン付き)。
- i18nRegions/videoCategories で市場(国×カテゴリ)マトリクスを管理。英語圏・アジア圏など多地域BGM市場の空白発見に使う。

### J-4. 投稿戦略最適化

- 追跡データの「投稿時刻×48h初速」相関をチャンネル/ジャンル別に集計し、`publish_time_jst` の推奨値を提案(自動変更はせず提案+ワンクリック適用)。投稿頻度の推奨(週◯本)も同様。

### J-0. スプレッドシート依存の全廃(ユーザー決定: 2026-07-04)

- 既存のGoogle Sheetsからのベンチマーク情報取得・表示機能(app_sheets.py経由のベンチ取込、関連UI)を**削除**し、Data API直取得に総入れ替え。
- ベンチマーク指定は**チャンネルURLの個別登録**に一本化(Phase C benchmark_set と同一動線)。URL→channels.list解決→uploads playlist→videos+batchGetStats で取込。
- 既存ベンチデータは可能な範囲でURL登録形式へ移行(channel_idが分かるものは自動移行、不明分は再登録案内)。
- 表示は現行の視覚スタイルを踏襲し、**チャンネルアイコン・チャンネル名・各種KPI(登録者・成長・48h初速・投稿頻度)** をカード/テーブルで整理。
- **UI決定プロセス**: gpt-image-2 でラフ案を複数生成→ユーザーと議論→確定後に実装(確定まではバックエンドとAPIのみ先行)。

### 実装順序と概算クォータ

J-0+J-2(バックエンド先行) → UIラフ議論・確定 → UI実装 → J-3 → J-1 → J-4。消費: J-2(≒50u/日) / J-3(≒30u/日) / J-1(≒1,200u/週) / J-4(追加なし)。合計でも既定1万/日の1〜2%程度。search予算はカタログで調整可能。

### コンプライアンス上の設計原則(全機能共通)

- 取得データはすべて fetched_at 付与+30日保持ルール適用(Phase I機構)。表示には「データ提供: YouTube」帰属。第三者提供なし(自社意思決定のみ)。コメントの書き込み系APIは使わない(読み取りのみ)。クォータメーターに feature タグを追加し機能別消費を可視化。

- 監査・割り当て増加フォームの回答ドラフト(日英): クライアント概要 / ユースケース / メソッド別・1日あたりの消費内訳(新コスト基準) / データ保持・削除の説明 / デモ手順。docs/QUOTA_AUDIT_APPLICATION.md として納品。申請の提出自体はユーザーのGoogleアカウントで行う。

## 制約(実装時の鉄則)

- 既存の `STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY` は変更しない(参照のみ)。
- app.py 変更後は `bash Python/start.sh` で再起動(uvicorn reload なし)。
- 検証: `python3 -m compileall -q Python` / `curl -s localhost:8888/openapi.json` / `studio.py --list` / `studio.py resolve <実在vol> --dry-run`。
- 破壊的変更(既存エンドポイント削除・リネーム)は禁止。追加のみ。
