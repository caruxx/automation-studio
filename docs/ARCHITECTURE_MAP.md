# Automation Studio Architecture Map

この文書は `Python/studio.py` / `Python/app.py` / `Python/app_pipeline.py` / `Python/routes.json` / LLM 関連モジュールを実読して作成した概略図です。推測ではなく、現コードの依存と分岐を優先しています。

## 1. システム構造図

```mermaid
flowchart LR
  %% 入口: AI/人間の正規入口は studio.py。Web UI と直接 CLI も存在する。
  Studio["studio.py<br/>routes.json を読み、vol/channel を解決して CLI/API へ委譲"]
  WebUI["Web UI :8888<br/>web/static の SPA。FastAPI を操作"]
  DirectCLI["直接 CLI<br/>app_pipeline.py / app_*.py を個別実行"]

  App["app.py FastAPI<br/>設定・動画・分析・タスク状態・ルーター集約"]
  Routes["routes.json<br/>intent, prefer, via_api_safe, parallelism の機械可読な真実"]
  Core["app_core.py<br/>共有設定・channel config・active_tasks・subprocess stream"]
  Pipeline["app_pipeline.py<br/>plan から upload までの step 実行本体"]

  Studio -->|"dry-run/実行解決"| Routes
  Studio -->|"prefer api"| App
  Studio -->|"prefer cli"| Pipeline
  WebUI -->|"HTTP/WebSocket"| App
  DirectCLI --> Pipeline
  DirectCLI -->|"個別実行"| Modules["app_*.py modules<br/>競合分析・SUNO・Premiere・Photoshop・YouTube 等"]
  App --> Core
  App -->|"run-pipeline / create-from-benchmark"| Pipeline
  Pipeline --> Modules

  Analysis["app_competitor.py / app_benchmark_analyze.py<br/>YouTube ベンチ取得と music_direction / visual_direction / メタ提案"]
  Seed["app_benchmark_seed.py<br/>seed 動画 outlier 計算・DSP/Gemini 音源分析・seed hint"]
  Proposer["claude_proposer.py<br/>タイトル・説明・タグ・翻訳。master_prompts 解決"]
  LLM["app_llm_runner.py<br/>Claude CLI → Codex CLI フォールバック"]
  Suno["suno_auto_create.py<br/>Playwright で SUNO 生成・Workspace DL・GhostWriter batch"]
  Tracks["app_process_tracks.py<br/>曲名リネーム・ffmpeg フェード/正規化"]
  ImagePrompt["app_image_prompt.py / codex_imagegen.py<br/>画像 prompt 正規化・gpt-image/Codex 生成"]
  Premiere["app_premiere.py / jsx_bundle.py / routers/premiere_photoshop.py<br/>Premiere 配置・AME 書き出し・Photoshop 合成"]
  YouTube["app_youtube.py / routers/youtube.py<br/>upload / snippet update / localizations"]

  Modules --> Analysis
  Modules --> Seed
  Modules --> Proposer
  Modules --> Suno
  Modules --> Tracks
  Modules --> ImagePrompt
  Modules --> Premiere
  Modules --> YouTube
  Analysis --> LLM
  Seed --> LLM
  Proposer --> LLM
  ImagePrompt -->|"Codex CLI fallback または Image API"| CodexCLI["Codex CLI<br/>codex exec / 画像生成 fallback"]
  LLM --> ClaudeCLI["Claude CLI<br/>claude -p"]
  LLM --> CodexCLI

  Suno --> SunoExt["SUNO<br/>Playwright 永続ブラウザ・Workspace"]
  YouTube --> YouTubeAPI["YouTube Data API<br/>動画取得・upload・snippet/localizations"]
  Seed --> Gemini["Gemini API<br/>seed 音源の実聴分析"]
  Premiere --> PremiereExt["Premiere + AME<br/>Pymiere / JSX Launcher / Media Encoder"]
  Premiere --> PhotoshopExt["Photoshop<br/>PSD 合成 UXP/CEP"]
  Tracks --> FFmpeg["ffmpeg / ffprobe<br/>音声処理・QA"]
  ImagePrompt --> OpenAIImage["OpenAI Image API<br/>gpt-image-2 when key exists"]
```

## 2. パイプライン Step 図

```mermaid
flowchart TD
  Plan["plan<br/>step_plan()<br/>competitor_analysis_cache + benchmark axes -> propose_suno_prompt -> plan.json<br/>skip: 通常実行では無し。--from-benchmark 時だけ先頭に入る。既存 plan.json + quality_score + suno_prompt なら再利用"]
  SunoStep["suno<br/>step_suno()<br/>plan/env/channel/global prompt -> suno_auto_create.py<br/>resource: SUNO browser + Claude/Codex GhostWriter<br/>skip/fail: prompt 不在は停止。APP_SUNO_AUTO_DOWNLOAD=0 で DL 後処理をしない"]
  Rename["rename<br/>step_rename()<br/>app_process_tracks.py<br/>resource: Claude/Codex title proposal + ffmpeg<br/>skip: rename-only では ffmpeg 無し。対象 MP3 不足は non-fatal 系"]
  Bg["bgimage<br/>step_bgimage()<br/>_build_bgimage_prompt() -> codex_imagegen.py<br/>resource: gpt-image-2/Codex CLI, benchmark refs<br/>skip: APP_BGIMAGE_DISABLE=1, persona 空, 既存 volN.png/jpg/source.jpg。APP_BGIMAGE_FORCE=1 で再生成"]
  Psd["psd_composite<br/>step_psd_composite()<br/>Photoshop PSD に背景 + scene text -> volN.jpg + サムネイル.jpg<br/>resource: Photoshop + scene text LLM<br/>skip: 既存 2 出力ありかつ force 無し"]
  Prem["premiere<br/>step_premiere()<br/>app_premiere.py / API / JSX で配置<br/>resource: Premiere<br/>skip: export_engine=ffmpeg なら premiere step はスキップ"]
  Export["export<br/>step_export()<br/>AME/Premiere export または app_ffrender.py<br/>resource: AME/Premiere or ffmpeg<br/>branch: export_engine=ffmpeg なら app_ffrender.py で premiere+export 置換"]
  QA["qa<br/>step_qa()<br/>ffprobe で解像度・尺・codec、任意 loudness<br/>resource: ffprobe/ffmpeg<br/>skip: APP_QA_DISABLE=1, mp4 無し, ffprobe 無し"]
  Meta["meta<br/>step_meta()<br/>claude_proposer gather_context -> titles/description/tags<br/>resource: Claude/Codex LLM<br/>writes: youtube_title/description/tags.txt"]
  Loc["localization<br/>step_localization()<br/>translate_metadata -> youtube_localizations.json<br/>resource: Claude/Codex LLM<br/>skip: localization_languages 未設定、title/description 不在"]
  Thumb["thumbnail<br/>step_thumbnail()<br/>_build_thumbnail_prompt() -> codex_imagegen.py -> thumbnail.png<br/>resource: gpt-image-2/Codex CLI<br/>skip: APP_THUMBNAIL_DISABLE=1, thumbnail.png/vol*.jpg/サムネイル.jpg 既存。Flow 指定は codex fallback"]
  Upload["upload<br/>step_upload()<br/>app_youtube.py / API<br/>resource: YouTube Data API<br/>skip: youtube_upload.json marker が現タイトル一致または72h以内。APP_FORCE_REUPLOAD=1 で無効"]

  Plan --> SunoStep --> Rename --> Bg --> Psd --> Prem --> Export --> QA --> Meta --> Loc --> Thumb --> Upload
```

## 3. プロンプトフロー図

```mermaid
flowchart TD
  MasterGlobal["共有 master_prompts.json<br/>global override"]
  MasterChannel["<channel>/.app_channel_config.json master_prompts<br/>channel override"]
  Hardcoded["claude_proposer.py hardcoded prompts<br/>空キー時 fallback"]
  ResolveMaster["_load_master_prompt()<br/>channel -> global -> hardcoded"]
  ApiMaster["app.py get_master_prompts()<br/>global + channel を UI/API に返す"]

  MasterGlobal --> ApiMaster
  MasterChannel --> ApiMaster
  MasterChannel --> ResolveMaster
  MasterGlobal --> ResolveMaster
  Hardcoded --> ResolveMaster

  CompetitorData["app_competitor<br/>YouTube Data API / benchmark cache<br/>topByViews, recentUploads, tags, stats"]
  Analyze["app_benchmark_analyze.analyze_with_claude()<br/>競合 summary + growth signals -> buzz_patterns, music_direction, visual_direction"]
  Cache["competitor_analysis_cache.json<br/>analysis + competitor_data"]
  SunoPrompt["propose_suno_prompt()<br/>music_direction + viewer_needs + seed hints -> SUNO prompt"]
  MetaPrompt["propose_with_analysis() / claude_proposer<br/>viewer_needs, keywords, underserved, songs, thumbnail, seed hints -> title/desc/tags"]

  CompetitorData -->|"上位/直近動画のタイトル・再生数・タグ"| Analyze
  Analyze -->|"music_direction / visual_direction"| Cache
  Cache -->|"music_direction + buzz_patterns"| SunoPrompt
  Cache -->|"viewer_needs / keywords / visual promise"| MetaPrompt
  ResolveMaster -->|"title_generation / description_generation / tags_generation template"| MetaPrompt

  SeedVideo["app_benchmark_seed.analyze_seed_video()<br/>YouTube context + code-computed outlier evidence"]
  SeedAudioDSP["dsp_music_profile()<br/>librosa BPM/key/density/texture"]
  SeedGemini["_analyze_seed_audio_gemini()<br/>Gemini 実聴 music_profile"]
  SeedAdj["claude/codex 調停<br/>_call_adjudication_engine() で矛盾整理"]
  SeedStore["benchmark/seed_analyses.json<br/>viewer_use_case, click_promise, safe_to_borrow, do_not_copy, music_profile"]
  SeedVideo -->|"観察データ + outlier_evidence"| LLMRun["app_llm_runner.run_llm()<br/>Claude -> Codex fallback"]
  LLMRun --> SeedStore
  SeedAudioDSP -->|"実測音響指標"| SeedAdj
  SeedGemini -->|"AI 実聴の抽象特徴"| SeedAdj
  SeedAdj --> SeedStore
  SeedStore -->|"seed_prompt_hint / seed_music_profile_hint"| SunoPrompt
  SeedStore -->|"クリック前の約束・コピー禁止・PDCA changed_element"| MetaPrompt

  BgPrompt["_build_bgimage_prompt()<br/>concept.txt -> benchmark concept -> thumbnail aggregate -> visual_direction -> persona"]
  VisionRefs["run_llm_vision()<br/>参照3枚の共通要素抽出"]
  CodexImage["codex_imagegen.py<br/>build_gpt_image2_prompt + reference-image -> background/thumbnail"]
  Cache -->|"visual_direction: time_of_day, atmosphere, composition, palette, avoid"| BgPrompt
  SeedStore -->|"safe_to_borrow / do_not_copy を背景方向に注入"| BgPrompt
  VisionRefs -->|"subject, lighting, color, mood の共通要素"| BgPrompt
  BgPrompt -->|"Subject/Background/Lighting/Style/Camera/Constraints"| CodexImage

  SunoGhost["suno_auto_create GhostWriter batch<br/>prompt + mode instrumental_filler -> N 曲 title/styles/lyrics"]
  Instrumental["instrumental_filler<br/>ボーカルなし・構造/スタイル補強"]
  SunoPrompt -->|"採用 prompt / plan.json / channel suno.prompt"| SunoGhost
  Instrumental -->|"lyrics/styles の instrumental 制約"| SunoGhost
  SunoGhost -->|"SUNO UI 入力値"| SunoExt["SUNO"]

  MetaPrompt --> LLMRun
  SunoPrompt --> LLMRun
  Analyze --> LLMRun
  VisionRefs --> LLMRun
```

## 4. リソース競合マップ

### CLAUDE.md のルール

| リソース | 使う処理 | 文書上の並列性 |
|---|---|---|
| SUNO ブラウザ | 楽曲生成・DL | 単一 |
| Premiere/AME | premiere・export | 単一 |
| Photoshop | psd_composite | 単一 |
| Claude/Codex LLM | meta・localization・scene_text・SUNO GhostWriter・bgimage | competition 注意 |
| ffmpeg | 楽曲後処理 | 並列可 CPU |

### routes.json との突き合わせ

| intent | routes.json parallelism | CLAUDE.md との判定 |
|---|---:|---|
| `suno`, `suno-auto`, `suno-download` | `per-machine:1` | 修正済み。SUNO ブラウザはマシン単位の単一リソースとして routes.json と CLAUDE.md が整合。 |
| `premiere`, `place-images`, `export`, `pipeline-from-premiere` | `per-machine:1` | 整合。Premiere/AME は単一。 |
| `psd` | `per-machine:1` | 整合。Photoshop は単一。 |
| `bgimage`, `thumbnail` | `global:2` | 概ね整合。ただし LLM/Codex quota は competition 注意。 |
| `meta`, `localization`, `propose` | `global:3` | 概ね整合。ただし Claude/Codex usage limit には注意。 |
| `rename`, `process`, `qa` | `global:4` | ffmpeg 並列可と整合。 |
| `pipeline` | `per-channel:1` | 部分不整合。pipeline 内に SUNO/Premiere/Photoshop 単一資源が含まれるため、別 channel の pipeline 同時実行は物理資源競合の可能性がある。 |

### 実装上の補足

```mermaid
flowchart LR
  Routes["routes.json parallelism<br/>intent scope/max_parallel"]
  Orchestrator["app_orchestrator.py<br/>_load_route_parallelism() / can_run()"]
  AppTasks["app.py / routers<br/>active_tasks + _ensure_not_running()"]
  Guard["parallel_guard.py<br/>opt-in flock wrapper"]

  Routes --> Orchestrator
  AppTasks -->|"Web API 実行中タスクの一部排他"| AppTasks
  Routes --> Guard
  Guard --> Locks["/tmp/automation_studio_locks/<scope>.lock<br/>suno-browser / premiere-ame / photoshop"]
```

`Python/parallel_guard.py` は既存挙動を変えない opt-in です。SUNO/Premiere/Photoshop の三大単一リソースは `suno-browser` / `premiere-ame` / `photoshop` に明示マップしています。

## コードと文書の不整合

1. 修正済み: `AGENTS.md` の AI サムネ記述を codex 一本化・Flow 指定時 codex フォールバックに更新。
2. 修正済み: `.claude/agents/image.md` の Flow / Nano Banana 2 前提を削除し、codex 一本化に更新。
3. 修正済み: `.claude/agents/pipeline.md` の `STEPS` に `localization` を追加。
4. 修正済み: `routes.json` の SUNO 系を `per-machine:1` に変更し、SUNO ブラウザ単一ルールと整合。
5. CLAUDE.md の pipeline 並列ルールに対し、`routes.json` の `pipeline` は `per-channel:1`。pipeline は内部に SUNO/Premiere/Photoshop を含むため、別 channel 同時実行は物理資源競合の可能性がある。
