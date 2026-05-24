# app-master-config: マスター設定（統合管理タブ）

プロンプト・SUNO/Flow パラメータ・スケジュール・書き出し・リモートアクセス・インポート/エクスポートを
**1 画面で集約管理**する「⚙ マスター設定」タブ。v2 で新設。

## 目的

v1 までは設定が以下 5 箇所に散在していた:

| 場所 | 責務 |
|------|------|
| `~/.config/{app_id}/dashboard_config.json` | チャンネル / ペルソナ / ライバル |
| `~/.config/{app_id}/suno_config.json` | SUNO プロバイダー / プロンプト / モード |
| `~/.config/{app_id}/benchmark_config.json` | ベンチマーク設定（v1 で分離） |
| `~/.config/{app_id}/prompts.json` | プロンプトプリセット |
| `{channel_folder}/_automation.json` | チャンネル別自動化 |
| `claude_proposer.py` ハードコード | タイトル/説明/タグ生成プロンプト（編集不可） |
| `flow_automation.py:60` | `DEFAULT_COUNT="x4"` 決め打ち |

→ 「プロンプトを調整したい」「Flow の枚数を変えたい」だけでコード編集が必要だった。

v2 では**物理ファイル分離は維持**（後方互換）しつつ、API 層で統合し、UI で 1 画面編集できるようにした。

## サイドバー

```
📊 ダッシュボード
🎬 コンテンツ
🎯 ベンチマーク
⚙ 自動化
──
⚙ マスター設定   ← v2 新規（新規ダッシュボードレイヤー）
基本設定          ← 既存（チャンネル基本・API キー・ペルソナ）
```

## 9 セクション構成

| # | セクション | 対応ファイル |
|---|-----------|-------------|
| 1 | 📝 プロンプト管理 | `master_prompts.json`（8 種を上書き可） |
| 2 | 🎵 SUNO 詳細 | `suno_config.json` + `master_settings.suno` |
| 3 | 🖼 Flow 生成 | `master_settings.flow`（default_count, batch_size, reference_image） |
| 4 | 📝 メタ生成 | `master_settings.meta`（title_count, description_target_chars, tags_target_count, fixed_tags） |
| 5 | 🎯 ベンチマーク | `benchmark_config.json` + 「日本語で再分析」「キャッシュクリア」 |
| 6 | ⏱ 自動化スケジュール | `schedule_jobs.json`（詳細は [app-schedule.md](./app-schedule.md)） |
| 7 | 📤 書き出し | `export_rules.json`（自動化タブへのショートカット） |
| 8 | 🌐 リモートアクセス | `auth_token.txt` + Tunnel URL（詳細は [app-remote-access.md](./app-remote-access.md)） |
| 9 | 💾 インポート/エクスポート | 全設定の JSON ダンプ・復元 |

## プロンプト管理（Section 1）

`~/.config/{app_id}/master_prompts.json` に 8 種のプロンプトを上書き保存可能。
**空文字列にするとハードコード（ソースコード内のデフォルト）にフォールバック**。

| キー | 上書き対象 | 場所 |
|------|----------|------|
| `title_generation` | `_TITLES_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L60-88 |
| `description_generation` | `_DESCRIPTION_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L91-126 |
| `tags_generation` | `_TAGS_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L129-154 |
| `competitor_analysis` | `analyze_with_claude` 内 | [Python/app_competitor.py](../Python/app_competitor.py) L335 付近（v3 で言語ハイブリッド化: descriptive=日本語 / シード=英語 / 数値=numeric） |
| `suno_from_analysis` | `propose_suno_prompt` | [Python/app_competitor.py](../Python/app_competitor.py) L446 |
| `flow_from_analysis` | `propose_flow_prompt` | [Python/app_competitor.py](../Python/app_competitor.py) L514 |
| `suno_from_persona` | `/api/suno/suggest-prompt` | [Python/app.py](../Python/app.py) `api_suno_suggest_prompt` |
| `imitate_evolve` | `/api/videos/.../suggest-imitate-evolve` | 徹底パクリ進化分析（[app-imitate-evolve.md](./app-imitate-evolve.md)） |

### フォールバック実装

[Python/claude_proposer.py](../Python/claude_proposer.py) では `_load_master_prompt(key, fallback)` ヘルパーで
ファイルから読み、空なら `fallback` （ハードコード）を使う。

```python
def _load_master_prompt(key: str, fallback: str) -> str:
    if _MASTER_PROMPTS_FILE.exists():
        data = json.loads(_MASTER_PROMPTS_FILE.read_text(encoding="utf-8"))
        v = (data.get(key) or "").strip()
        if v: return v
    return fallback
```

**placeholder `{persona}`, `{songs}`, `{count}` 等はそのまま残すこと** — format() が失敗する。

v3 で `_TITLES_PROMPT` / `_DESCRIPTION_PROMPT` / `_TAGS_PROMPT` に **`{benchmark_section}`** プレースホルダが追加された。カスタムプロンプトを書く場合は、視聴者文脈を活かすために `{benchmark_section}` を必ず含めること（無くても format() は通るが、ベンチマーク連動の効果が消える）。

## SUNO 詳細（Section 2）

| 項目 | 保存先 | 既定 |
|------|--------|------|
| プロンプト本文 | `suno_config.prompt` | （空） |
| 生成回数 | `suno_config.loop_count` | 5 |
| 間隔（秒） | `suno_config.loop_interval_sec` | 180 |
| 一括生成 | `suno_config.loop_batch` | false |
| Workspace パターン | `master_settings.suno.workspace_pattern` | `{channel}_vol{vol}` |
| DL 待機秒 | `master_settings.suno.dl_wait_sec` | 30 |
| リトライ回数 | `master_settings.suno.retry_count` | 2 |

**設定タブの SUNO セクションと同じ値**（双方向同期）。マスター側の方が詳細項目を扱う。

## Flow 生成（Section 3）

`master_settings.flow.default_count` を 1〜8 で設定すると、
[Python/flow_automation.py](../Python/flow_automation.py) の `_master_flow_count()` が起動時に参照し、
`DEFAULT_COUNT="x4"` を上書きする。

```python
def _master_flow_count() -> str:
    p = Path.home() / ".config/{app_id}/master_settings.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        n = int((data.get("flow") or {}).get("default_count") or 0)
        if 1 <= n <= 8: return f"x{n}"
    return DEFAULT_COUNT
```

## API

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/master` | 全セクション統合返却 |
| PUT | `/api/master` | `{section, patch}` で透過更新 |
| GET | `/api/master/export` | 全設定の JSON ダンプ（バックアップ用） |
| POST | `/api/master/import` | JSON 復元（`overwrite: bool` で完全上書き vs マージ） |

### セクション一覧（PUT 時の `section` 値）

- `prompts` — master_prompts.json
- `settings.suno` / `settings.flow` / `settings.meta` / `settings.remote` — master_settings.json の各サブ
- `suno` — suno_config.json
- `benchmark` — benchmark_config.json
- `export` — export_rules.json

### 例: Flow 枚数を x2 に

```bash
curl -X PUT http://localhost:8888/api/master \
  -H 'Content-Type: application/json' \
  -d '{"section":"settings.flow","patch":{"default_count":2}}'
```

### 例: タイトル生成プロンプトを上書き

```bash
curl -X PUT http://localhost:8888/api/master \
  -H 'Content-Type: application/json' \
  -d '{"section":"prompts","patch":{"title_generation":"新しいタイトル生成プロンプト..."}}'
```

### 例: 既定に戻す（空文字を送る）

```bash
curl -X PUT http://localhost:8888/api/master \
  -H 'Content-Type: application/json' \
  -d '{"section":"prompts","patch":{"title_generation":""}}'
```

## インポート/エクスポート（Section 9）

### エクスポート
```bash
curl http://localhost:8888/api/master/export > app_settings_$(date +%Y%m%d).json
```

返却 JSON は以下を含む:
- `dashboard` / `suno` / `benchmark` / `channels` / `master_prompts` / `master_settings` / `export_rules`
- `schema_version`, `exported_at`

### インポート
```bash
curl -X POST http://localhost:8888/api/master/import \
  -H 'Content-Type: application/json' \
  -d "{\"data\": $(cat app_settings_20260424.json), \"overwrite\": false}"
```

- `overwrite: false`（既定）— 既存設定とマージ
- `overwrite: true` — 完全上書き（auth_token / channel_folder も含むため注意）

**別 PC への移行**、**バックアップ/リストア**、**チャンネル追加時の初期設定複製** に使える。

## フロント実装

| 関数 | 役割 |
|------|------|
| `loadMaster()` | `/api/master` を叩いて 9 セクションを描画 |
| `saveMasterPrompt(key)` | プロンプト個別保存 |
| `saveMasterSection(sec)` | SUNO / Flow / Meta / Remote セクション単位保存 |
| `exportMaster()` | ブラウザから JSON ダウンロード |
| `importMasterModal()` / `doImportMaster()` | ファイル選択 → インポート |

ページ: [web/static/index.html](../web/static/index.html) `p-master` 新設。サイドバー → `⚙ マスター設定`。

## 設計原則

- **物理ファイルは分離維持**: 各スクリプトが CLI 単独でも動作する（DB を持たない SPEC.md 設計原則）
- **空 = デフォルトフォールバック**: プロンプトを空にするとハードコードに戻る。リセットが安全
- **後方互換**: v1 の設定ファイルはそのまま使える。master_* は追加レイヤー
