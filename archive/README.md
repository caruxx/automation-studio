# archive

段階 7（2026-10-27）で、どこからも参照・起動されていないことを確かめたコードと、現行の正本ではない設計文書を移した場所です。削除はしていません。動かす前提のコードではありません。

| 元の場所 | 移した先 | 理由 |
|---|---|---|
| `Python/app_photos_picker.py` | `archive/Python/` | import も起動も無い（Google Photos ピッカーの試作） |
| `Python/app_vlog_plan.py` | `archive/Python/` | import も起動も無い（vlog 企画の試作） |
| `Python/save_auth.py` | `archive/Python/` | Flow 撤去（D8）後の残骸。SECURITY_NOTES で整理対象と記載 |
| `Python/flow_userscript.js` | `archive/Python/` | Flow 撤去（D8）後の残骸 |
| `app_pipeline._run_thumbnail_eval_loop` | `archive/Python/app_pipeline_thumbnail_eval_loop.py` | 段階 5 の AI 選定（`thumbnail_selection.py`）に置き換わり、呼ばれていない |

`Python/suno_queue.py`（複数 Workspace キュー）はコードからの参照は無いが、手で CLI として使う運用があったため移していない。

現行の正本ではない設計文書は `docs/archive/` にあります（`docs/archive/README.md`）。
