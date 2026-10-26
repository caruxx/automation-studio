# app-thumbnail: サムネ AI 選定（パイプライン STEP）

> 実装: `Python/app_pipeline.py` `step_thumbnail` / `Python/thumbnail_selection.py`（選定の中核）/ `Python/app_image_composite.py` `compose_candidate` / `Python/app_thumbnail_scoring.py` `score_thumbnails` / `Python/codex_imagegen.py`
> STEP キー: `thumbnail`（`STEPS` 内）。仕様: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §3.6

## 目的

投稿に使うサムネを、AI が候補を作って採点し 1 枚に確定する。投稿（upload）は **確定済みの `thumbnail_selected.jpg` だけ** を使う。

## 流れ

1. 新しい背景画像を候補数ぶん生成する（`codex_imagegen.run`、1 プロンプト 1 枚、ファイル名 `a{attempt}_bg{i}`）。
2. 各背景をチャンネル設定 `thumbnail_composition` で文字合成し、候補にする（`compose_candidate`）。
3. 候補を `score_thumbnails` で採点する（ベンチマーク参照画像つき）。
4. しきい値を満たす最良の候補を採用し、`thumbnail_selected.jpg` にコピーしてハッシュを再確認する。
5. 不合格なら評価の改善点を次の試行のヒントにして再生成する。

既定の上限は次のとおり。

| 項目 | 既定 |
|------|------|
| 試行回数 | 3 |
| 1 試行あたりの候補 | 4 |
| 候補の総数上限 | 12 |
| 採点しきい値 | 80 |
| 予測 CTR しきい値 | 7.0 |
| 類似度の上限 | 25 |

## 出力

| パス | 内容 |
|------|------|
| `thumbnail_candidates/<run_id>/a<N>/a<N>_<i>.jpg` | 候補画像。試行ごとにコンタクトシートも出す |
| `thumbnail_selected.jpg` | 採用画像。投稿はこれだけを使う |
| `.thumbnail_selection.json` | 選定記録 |

選定記録の主なキーは `candidate_ids, candidate_hashes, selected_id, selected_hash, rejected_reasons, model, model_enforced, backend, judge_backend, thresholds, predicted_ctr, selection_revision, history`。

`サムネイル.jpg` と `vol{N}.jpg` は書き換えない。書き出し（render）の入力を変えないためである。

## 完了判定

`thumbnail_selection.verify` が次をすべて満たすときだけ完了とする。

- 状態が `selected` で、`selected_id` が候補一覧にあり、現在の run のディレクトリ配下にある。
- `thumbnail_selected.jpg` が存在し、そのハッシュが `selected_hash` と候補ハッシュに一致する。

## 全候補不合格のとき（品質待ち）

全試行で採用候補が出なければ、工程は品質待ちで止まる。

- パイプラインは保留「サムネ品質待機」を報告し、終了コード 79 で止まる。
- オーケストレータは `SELECTION_PENDING` として扱い、同じ入力のまま再実行しない。
- 採点そのものの失敗は `score_failure`、生成基盤の失敗は `generation_failure` として区別し、再生成しない。

人が候補を選んで確定するには次を使う。

```bash
python3 Python/studio.py thumbnail-select --vol <N> --adopt <run_id>/a<N>/a<N>_<i>.jpg
```

`--adopt` なしで実行すると選定をやり直す。`--dry-run` は実行内容だけを表示する。終了コードは成功 0、品質待ち 79、失敗 1、引数誤り 2。

## 画像モデル

チャンネル設定 `image_model`（既定 `gpt-image-2`）で背景生成のモデルを決める。

- `OPENAI_API_KEY` があれば API 経路でそのモデルを使い、記録の `model_enforced` を真にする。
- Codex CLI 経路ではモデルを強制できないため、既定以外を指定するとエラーにする。

## 環境変数

| 変数 | 役割 |
|------|------|
| `APP_THUMBNAIL_RESELECT=1` | 確定済みでも選定をやり直す |
| `APP_THUMBNAIL_DISABLE` | 無視する（警告のみ）。投稿に確定サムネが必須のため |

## 投稿側の扱い

- `app_youtube.find_thumbnail` は選定記録があれば検証済みの `thumbnail_selected.jpg` だけを返す。記録がなければ旧来の `サムネイル.*` / `thumbnail.*` を使う。
- 選定記録があるか VPS 連携が有効なとき、検証に通らなければ投稿を始めずに終了コード 1 で止める。

## 段階 5 以前の vol（移行）

- 旧 `step_thumbnail` は常に成功を記録していたため、thumbnail だけは台帳の done で完了を補わない。未投稿の旧 vol は投稿前に選定が走る。
- 投稿済み（`youtube_upload.json` に video_id あり）の旧 vol は、選定記録がなくても snippet 更新ができる。
- 投稿途中で再開待ちの旧 vol は、VPS 連携時に確定サムネが必要になる。反映は再開待ちの投稿が無いときに行う。
- UI で手作りした `サムネイル.jpg` をそのまま投稿に使う経路は無い。使いたい場合は選定の候補から `--adopt` で確定する。

## 関連

- [app-bgimage.md](./app-bgimage.md) — 背景画像（`vol{N}` 用。サムネ候補の背景とは別に生成）
- [image.md](./image.md) — 画像合成（`サムネイル.jpg` / `vol{N}.jpg`）
- [app-image-select.md](./app-image-select.md) — `vol{N}.jpg` リネーム
