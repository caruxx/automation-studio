# app-premiere: Premiere Pro 自動配置（Pymiere版）

Pymiere経由で Premiere Pro を完全自動制御し、BGM動画のタイムライン構成を一括生成するスキル。

## 概要
1. `.prproj` を自動で開く（読み込み完了を検知、**必須**）
2. 自動配置くんJSXを送信（ダイアログなし、`selected_images.json` 優先）
3. タイムラインの実測値から正確なSRT字幕を生成
4. タイムコード情報を書き出し（LOOPマーカー付き）
5. SRTをキャプショントラックに自動配置

## 実行方法

### Web ダッシュボード（推奨）

**動画詳細 → 書き出しタブ** から、時・分・秒を指定して「▶ この動画でPremiere自動配置を実行」
- `video_name` が指定されるため `vol_vol{N}.prproj` を自動オープン → JSX 送信
- 「完了後に書き出し」チェックで Media Encoder キューまで一気に進む

**Premiere ページ** からは、対象動画セレクタで明示的に動画を選ぶか、
空のままにして **現在開いている** プロジェクトに JSX を送るモードで実行可。

### コマンドライン

```bash
# フル実行（3時間、プロジェクト自動オープン）
python3 app_premiere.py --duration 10800 --project /path/to/vol_vol73.prproj

# 時間指定のみ（プロジェクトは手動で開いておく）
python3 app_premiere.py --duration 7200

# 字幕・タイムコードだけ再生成
python3 app_premiere.py --regenerate-srt

# 接続確認のみ
python3 app_premiere.py --check
```

## プロジェクト未オープン時の挙動

JSX 先頭の `assertPremiere()` が `app.project.rootItem` を検査し、無ければ
```
Premiere Pro 上で実行してね（#target premierepro）
```
とアラートして終了する。**Web から実行する場合は必ず `video_name` を渡して
自動オープン経由にすること**。これが Web フローの基本。

## 処理フロー（5ステップ）

```
[1/5] プロジェクトオープン + 読み込み完了検知
       → open コマンド → Pymiere でポーリング → 完了検知

[2/5] JSX 自動配置くん実行
       → ダイアログ入力を目標時間に自動置換
       → JSX内のSRT生成・タイムコード書き出しは無効化（Python側で正確に生成）
       → $.evalFile() で一時ファイルを実行

[3/5] タイムライン実測値からクリップ情報取得
       → Pymiere API で全オーディオクリップの start/end を取得
       → ファイル名からタイトル抽出（.mp3除去、z_プレフィックス除去）

[4/5] 正確なSRT + タイムコード生成
       → SRT: タイムライン実測値ベース（ズレなし）
       → タイムコード: LOOPマーカー自動挿入

[5/5] SRTをPremiereにインポート → キャプショントラックに配置
       → createCaptionTrack API で配置
```

## JSX の処理内容（自動配置くん）

| 処理 | 内容 |
|------|------|
| MP3配置 | music/*.mp3 をA1にループ配置（z_付き優先、通常はランダム） |
| 動画素材 | audio-spectrum01.mp4 をV2にループ配置（不透明度20%、スケール20%、ルミナンスキー） |
| **画像配置** | V1 に配置。**`selected_images.json` 優先**、なければ `vol{N}.png`/`vol{N}-1.png` にフォールバック（詳細は下記） |
| ハードリミッター | 全Aトラックに -1dB Hard Limiter |
| フェードアウト | 終了20秒前から -96dB へキーフレーム |

### 画像配置ロジック（selected_images.json 対応）

```
0s -------- 5s -------- 30s ----------- End
[   main   ][ sub[0] or main ][ sub[0..N-1] を N等分 ]
```

- JSON パースは ExtendScript 制約で正規表現（`"main":"..."`, `"sub":[...]` を手動抽出）
- 実ファイル存在チェックを通ったものだけ採用
- どちらも見つからなければ `vol{N}.png` / `vol{N}-1.png` にフォールバック

契約の詳細は [app-image-select.md](./app-image-select.md) 参照。

### 画像なしで先に自動配置 → 後から画像だけ貼る

画像ファイルが間に合わない場合でも音声/字幕だけ先に配置して書き出し準備を進められる:

1. 画像未配置のまま JSX 実行 → `IMAGES_NONE` ログだけ残して音声/字幕は配置完了
   （以前の `alert("画像ファイルが見つかりませんでした")` はスキップ扱いに修正済み）
2. 後日サムネ/背景画像を配置 → 動画詳細「画像」タブ「🖼 画像を後から配置」ボタン
3. 専用 JSX [_place_images_only.jsx](../../DEV/Script/_place_images_only.jsx) が V1 の既存画像を削除して再配置

API: `POST /api/premiere/place-images {video_name}` → `app_premiere.py --images-only --project <prproj>`

## 前提条件

- Premiere Pro が起動していること（プロジェクトは閉じていてOK。`--project` で自動オープン）
- Pymiere + Premiere Link パネルがインストール済み（`setup.sh` または `cep_extension/install.sh` で自動セットアップ）
- music/ フォルダにMP3が配置済み（`app-rename-audio` 完了後）
- 画像ファイルが配置済み（`selected_images.json` or `vol{N}.png`）

## スクリプトファイル

```
共有ドライブ/DEV/_claude/Python/app_premiere.py       ← メインスクリプト
共有ドライブ/DEV/Script/_[自動配置くん]premiere_long.jsx  ← JSX本体
```

## 出力ファイル

```
{動画フォルダ}/subtitles_{num}.srt              ← 正確なSRT字幕
{動画フォルダ}/music_time_code_info_{num}.txt    ← タイムコード（LOOP付き）
```

## トラブルシューティング

| 症状 | 原因 | 対策 |
|------|------|------|
| `Premiere Pro 上で実行してね` アラート | プロジェクト未オープン | Web の書き出しタブから `video_name` 付きで実行、または `--project` 指定 |
| 接続できない | Premiere Link 未インストール | `bash cep_extension/install.sh` 実行 |
| musicが見つからない | getFilesのコールバック非対応 | evalFile方式で実行（対応済み） |
| 字幕がはみ出す | JSX内蔵のSRT生成が不正確 | Python版SRT生成を使用（対応済み） |
| AppleScript タイムアウト | Premiere の AppleEvent 制限 | Pymiere 経由で実行（対応済み） |
| 画像が意図通りに並ばない | `selected_images.json` の不整合 | 「画像」タブで再選択 or リセットで `vol{N}` フォールバック |
