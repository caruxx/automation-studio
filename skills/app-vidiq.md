# app-vidiq: VidIQ 分析の取り込みとメタ生成への反映（段階 6）

> 実装: `Python/vidiq_adapter.py`（MCP クライアント・正規化・キャッシュ）/ `Python/studio.py` `run_insights_refresh` / `Python/app_pipeline.py` `_vidiq_meta_context`（メタ生成への注入）/ VPS `app/api/worker.py` `POST /api/worker/insights/vidiq`
> 仕様: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §3.7

## 目的

VidIQ MCP の分析（キーワード調査・伸びている動画など）を取り込み、タイトル・説明文・タグの生成に反映する。VidIQ が使えないときも、メタ生成と投稿は止めない。

## 使い方

```bash
python3 Python/studio.py config set channel.vidiq_keywords '["lofi jazz","study music"]' --channel <id>
python3 Python/studio.py insights-refresh --channel <id> --json
```

- `--dry-run`: VidIQ に問い合わせず、設定と期限だけを表示する。
- `--force`: 期限内でも取り直す。クレジットを消費する。調べる語や呼び出しを変えたときも、期限内は前回の結果を返すので `--force` を付ける。
- `--max-calls N`: 1 回の更新で呼ぶツールの上限。既定 3、1〜10。

終了コードは、取得済みまたは期限内が 0、取得失敗や VPS への送信失敗が 1、使い方の誤りが 2。

## 準備（本人が行う）

1. app.vidiq.com のアカウント設定で MCP の API キー（読み取り専用）を発行する。
2. 次のどちらかに置く。repo には置かない。
   - 環境変数 `VIDIQ_MCP_API_KEY`
   - `~/.config/automation-studio/vidiq.json` に `{"api_key": "..."}`（`chmod 600` が必須。他人が読める権限だと読み込まない）

接続先は既定で `https://mcp.vidiq.com/mcp`。環境変数 `VIDIQ_MCP_ENDPOINT` で変えられる。

## 何を呼ぶか

ツール名と引数は推測で決めない。`tools/list` にあり、`inputSchema.required` を満たせる呼び出しだけを行う。

| 設定 | 呼び方 |
|---|---|
| `channel.vidiq_requests`（優先） | `[{"tool": ..., "arguments": {...}, "category": ...}]` をそのまま呼ぶ。一覧にないツールや必須引数の欠けた呼び出しは飛ばす |
| `channel.vidiq_keywords` | 各語を `keyword_research` の唯一の必須文字列引数に入れて呼ぶ。スキーマが合わなければ呼ばない |

## 保存と根拠

- 取得結果は 24 時間有効。期限内は再取得しない。
- Mac のローカルキャッシュ `~/.config/automation-studio/vidiq/<channel>.json`（権限 600）に原文と正規化結果を保存する。
- VPS 連携時は、正規化した要点だけを VPS の `insights`（`source=vidiq`）に送り、チャンネル単位で入れ替える。
- 根拠 id は `vidiq:<tool>:<結果本文の sha256 先頭 16 桁>` で、実際に受け取った結果から作る。動画 ID は結果に現れたものだけを拾う。

## メタ生成への反映

- VPS 連携時は、vol 応答の学び（`insights_hint`）と VidIQ の鮮度（`insights_vidiq`）を使う。無効時はローカルキャッシュの期限内のものを使う。
- メタの記録 `youtube_meta.json` は `{title, description, tags, evidence_ids, insights_degraded, degraded_reason}`。
- `evidence_ids` は、プロンプトに渡したヒントに全文が入った VidIQ の学びの id。LLM が実際に使ったかは判定しない。
- `youtube_meta.json` は工程の完了判定の材料に含めない。取得時刻の違いで後段を失効させないためである。

## 使えないとき（劣化）

| `degraded_reason` | 状況 | 通知 |
|---|---|---|
| `not_configured` | チャンネルに VidIQ の設定が無い、または API キーが無い | 出さない |
| `not_fetched` | VPS 側で一度も取り込みが無い（VPS はチャンネルの設定を知らない） | 出さない |
| `unavailable` | 設定済みなのに取り込みが無い、またはヒントに入らなかった | info |
| `expired` | 取得から 24 時間を過ぎた | info |
| `fetch_failed` | 取得の失敗 | info |

通知は VPS 連携時は VPS がメタ受領時に 1 回積み、VPS 無効時は Mac から出す。どちらでもメタはチャンネルの方針（persona・言語）とベンチマークだけで生成する。

## 費用の注意

調査系のツールはおおむね 1 回 5 クレジットを消費する（2026-10-27 時点の VidIQ の公開文書）。既定では 1 回の更新で最大 3 回呼ぶ。定期取得はしない。

## 未検証

- API キーを送るヘッダ名は文書に無い。MCP の標準どおり `Authorization: Bearer` で送っている。
- 実際のツールの入力スキーマと結果の形。
- 実接続とクレジットの消費は、本人のキー発行後に確かめる。

## 関連

- [app-workflow.md](./app-workflow.md) — STEP 8 meta
