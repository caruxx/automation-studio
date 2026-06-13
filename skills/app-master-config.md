# app-master-config: マスター設定（統合管理タブ）

プロンプト・SUNO パラメータ・スケジュール・書き出し・リモートアクセス・インポート/エクスポートを
**1 画面で集約管理**する「⚙ マスター設定（詳細設定）」タブ。v2 で新設。
（D12 で `master_settings.json` は完全廃止 — suno 系は `suno_config.json`、tunnel_url は `dashboard_config.json`、その他は per-channel 設定へ移行。D8 で Flow/Midjourney は撤去され画像生成は codex 一本化。）

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
| `flow_automation.py:60`（D8 で本体ごと撤去済み） | `DEFAULT_COUNT="x4"` 決め打ち |

→ 「プロンプトを調整したい」「生成枚数を変えたい」だけでコード編集が必要だった。

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

## セクション構成（v2 当初 9 → D8/D12 後は実質 6）

| # | セクション | 対応ファイル |
|---|-----------|-------------|
| 1 | 📝 プロンプト管理 | `master_prompts.json`（7 種を上書き可。チャンネル別保存 → グローバルにフォールバック） |
| 2 | 🎵 SUNO 詳細 | `suno_config.json`（基本設定と同期） |
| 3 | Flow 生成 | **D8/D12 で撤去**（Flow 機能撤去 + `master_settings.flow` 廃止。画像生成は codex 一本化） |
| 4 | メタ生成 | **D12 で撤去**（`master_settings.meta` は実処理に未配線の死蔵設定だったため廃止） |
| 5 | ベンチマーク | UI カードは撤去（競合分析キャッシュカードに集約）。API の `benchmark` セクションは存続 |
| 6 | ⏱ 自動化スケジュール | `schedule_jobs.json`（詳細は [app-schedule.md](./app-schedule.md)） |
| 7 | 📤 書き出し | `export_rules.json`（自動化タブへのショートカット） |
| 8 | 🌐 リモートアクセス | `auth_token.txt` + Tunnel URL（D12 で `dashboard_config.json` の `tunnel_url` に移送。詳細は [app-remote-access.md](./app-remote-access.md)） |
| 9 | 💾 インポート/エクスポート | 全設定の JSON ダンプ・復元 |

## プロンプト管理（Section 1）

7 種のプロンプトを上書き保存可能（アクティブチャンネルの設定に保存され、グローバル `~/.config/{app_id}/master_prompts.json` にフォールバック）。
**空文字列にするとハードコード（ソースコード内のデフォルト）にフォールバック**。

| キー | 上書き対象 | 場所 |
|------|----------|------|
| `title_generation` | `_TITLES_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L60-88 |
| `description_generation` | `_DESCRIPTION_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L91-126 |
| `tags_generation` | `_TAGS_PROMPT` | [Python/claude_proposer.py](../Python/claude_proposer.py) L129-154 |
| `competitor_analysis` | `analyze_with_claude` 内 | [Python/app_competitor.py](../Python/app_competitor.py) L335 付近（v3 で言語ハイブリッド化: descriptive=日本語 / シード=英語 / 数値=numeric） |
| `suno_from_analysis` | `propose_suno_prompt` | [Python/app_competitor.py](../Python/app_competitor.py) L446 |
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

**設定タブの SUNO セクションと同じ値**（双方向同期）。
旧 `master_settings.suno`（workspace_pattern / dl_wait_sec / retry_count）は D12 で廃止 — DL 待機秒等は自動化タブ・実行リクエスト側（`suno_config` / API パラメータ）に統合済み。

## Flow 生成（Section 3）

D8 で撤去（Flow 機能ごと削除・画像生成は codex 一本化）。`master_settings.flow` も D12 で廃止。

## API

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/master` | 全セクション統合返却 |
| PUT | `/api/master` | `{section, patch}` で透過更新 |
| GET | `/api/master/export` | 全設定の JSON ダンプ（バックアップ用） |
| POST | `/api/master/import` | JSON 復元（`overwrite: bool` で完全上書き vs マージ） |

### セクション一覧（PUT 時の `section` 値）

- `prompts` — master_prompts（アクティブチャンネル別保存 → グローバルフォールバック）
- `suno` — suno_config.json
- `benchmark` — benchmark_config.json
- `export` — export_rules.json
- `remote` — dashboard_config.json の `tunnel_url`（D12 で master_settings から移送）

（旧 `settings.suno` / `settings.flow` / `settings.meta` / `settings.remote` は D12 の master_settings.json 廃止に伴い削除）

### 例: リモートアクセスの Tunnel URL を保存

```bash
curl -X PUT http://localhost:8888/api/master \
  -H 'Content-Type: application/json' \
  -d '{"section":"remote","patch":{"tunnel_url":"https://example.trycloudflare.com"}}'
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
- `dashboard` / `suno` / `benchmark` / `channels` / `master_prompts` / `export_rules`（`master_settings` は D12 廃止で含まれない）
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
| `saveMasterSection(sec)` | SUNO / Remote セクション単位保存（Flow / Meta は D8/D12 で撤去） |
| `exportMaster()` | ブラウザから JSON ダウンロード |
| `importMasterModal()` / `doImportMaster()` | ファイル選択 → インポート |

ページ: [web/static/index.html](../web/static/index.html) `p-master` 新設。サイドバー → `⚙ マスター設定`。

## 設計原則

- **物理ファイルは分離維持**: 各スクリプトが CLI 単独でも動作する（DB を持たない SPEC.md 設計原則）
- **空 = デフォルトフォールバック**: プロンプトを空にするとハードコードに戻る。リセットが安全
- **後方互換**: v1 の設定ファイルはそのまま使える。master_prompts は追加レイヤー（master_settings は D12 で廃止）
