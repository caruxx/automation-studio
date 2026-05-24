# app-image-select: 背景画像の選択 + Premiere JSX 連動

動画フォルダ内の画像から **メイン1枚 + サブ N枚** を UI で選び、
`selected_images.json` に保存 → Premiere JSX が優先読み込みしてタイムラインに配置する仕組み。

## データ契約: `selected_images.json`

保存場所: `<動画フォルダ>/selected_images.json`

```json
{
  "main": "vol74.jpg",
  "sub": ["bg_city.png", "bg_night.png", "bg_rain.png"]
}
```

- `main`: 0-5s に配置される画像（ファイル名のみ、相対パス不可）
- `sub[]`: 30秒以降を N 等分して順番に配置（5-30s は sub[0] or main にフォールバック）
- **ファイルが存在しない場合は無視**（部分的な不整合は許容）

## 判定ロジック

### JSX 側: [Script/_[自動配置くん]premiere_long.jsx](../Script/_[自動配置くん]premiere_long.jsx)

1. `<workDir>/selected_images.json` を探す
2. 存在すれば: 正規表現で `"main": "..."` と `"sub": [...]` を抽出
   （ExtendScript に JSON パーサがないため）
3. 実ファイル存在チェック → 見つかったものだけ採用
4. **選択なし or 全て見つからない場合**: 従来通り `vol{N}.png` / `vol{N}-1.png` にフォールバック

### Python / Web 側

- `GET /api/videos/{name}/images` — フォルダ内の `.jpg/.jpeg/.png` 一覧、サムネ判定、現在の kind (`main`/`sub`/`none`)
- `GET /api/videos/{name}/image-file/{fn}` — プレビュー配信（パストラバーサル対策あり）
- `PUT /api/videos/{name}/selected-images` — 保存
- `DELETE /api/videos/{name}/selected-images` — リセット（初期値に戻す）

一覧 API には `has_images_selected` ブール値が付与され、ワークフロー 6 工程の「画像選択」判定に使われる。

## タイムライン配置ルール

```
0s -------- 5s -------- 30s ----------- End
[   main   ][ sub[0] or main ][ sub[0..N-1] を N等分 ]
```

- `totalDuration < 5s`: main のみ
- `5s ≤ totalDuration < 30s`: main + (sub[0] or main)
- `totalDuration ≥ 30s`:
  - サブが1枚のみ: 5-End 区間に sub を1枚伸ばす
  - サブが複数: 30-End を `N = sub.length` 等分して順に配置

## UI（動画詳細「画像」タブ）

- グリッド表示（16:9 サムネ、ファイル名、サイズ）
- クリックで **なし → MAIN → SUB → なし** をトグル
- メイン=赤枠「MAIN」バッジ、サブ=青枠「SUB 1/2/…」番号バッジ、YouTube サムネ候補=紫「サムネ」バッジ
- 「選択を保存」→ PUT、「リセット」→ DELETE
- サブは **選んだ順に番号が付く**（順序が配置順に直結）

## 注意

- `main` にセットした画像を「サムネ」として使用したい場合は、ファイル名を `vol{num}.jpg` にリネームすれば
  自動で YouTube サムネイルにも採用される（`app-youtube-upload.md` 参照）
- `selected_images.json` はバージョン管理せず、動画フォルダと一緒に Google ドライブで同期
- JSX 側でパス区切りが `/` に統一される（ExtendScript 既定の Windows パス対策）
