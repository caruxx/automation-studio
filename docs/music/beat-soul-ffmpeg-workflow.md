# 新チャンネルの制作構成：Adobeなし

2026-09-29のユーザー指定。新しいBeat Soul系チャンネルではPhotoshop、Premiere Pro、Adobe Media Encoderを使わない。画像は既存のImage2 Layout Studio（Canvas／Pillow）、音声・動画・字幕・タイムスタンプはFFmpegで処理する。正式なチャンネル名と保存先は未確定のため、既存チャンネルを切り替えず、再利用用の構成を用意している。

## 制作の流れ

1. `docs/music/beat-soul-recipe.md` と `config/music_presets/beat-soul-4min.json` を基準にSUNO生成。Custom Durationは240秒。BGMの上質さを理由に、ビートや冒頭の歌フックを弱くしない。
2. 各タイトルの2テイクをブラウザで聴き比べ、採用する方にハートを付ける。全タイトルの選択とダウンロードが完了するまで後続工程を止める。人が決めた採用結果に従って音声処理し、`music/`へ保存する。
3. オリジナルの背景画像を生成し、`vol{N}.png` または `vol{N}_source.jpg` を保持する。競合の実画像を生成用の参照素材に混ぜない。
4. 画像合成で動画用背景 `vol{N}.jpg` と `サムネイル.jpg` を1920×1080で作る。トリミング、暗さ、周辺の陰影、文字入れはPillow。必要な手直しは画像エディタで行う。既に両画像がある場合は再利用する。
5. 動画の曲順・画像・必要な曲名表示を設定し、FFmpegでMP4を作る。採用音源の実際の尺とタイムラインから字幕とタイムスタンプを作る。楽曲生成の指定240秒を足し算する方式にはしない。
6. MP4の尺、解像度、音声と映像、サムネを確認。概要欄のTracklistへタイムスタンプを取り込む。公開は別工程。

曲名末尾の数値テイク識別子 `(1)`／`（２）` は表示時に除去する。音源のID・選択記録は保持し、数値ではない `(Live)` 等は残す。重複タイトル防止のDBとバッチのログも引き続き使用する。

## 設定と呼び出し

構成プリセットは `config/channel_presets/beat-soul-ffmpeg.json`。チャンネルの `.app_channel_config.json` に必要な項目を統合する。他チャンネルの設定ファイルを丸ごとコピーしない。

```json
{
  "production_mode": "ffmpeg_only",
  "export_engine": "ffmpeg",
  "template_prproj": "",
  "template_psd": "",
  "scene_text_enabled": false,
  "thumbnail_composition": {
    "title": "",
    "headline": "",
    "darken": 0,
    "vignette": 0
  }
}
```

新規チャンネル登録画面で「Adobeなし（FFmpeg＋画像エディタ）」を選ぶ。API登録は `POST /api/channels` の `production_mode: "ffmpeg_only"`。旧クライアントがこの項目を送らない場合は従来の構成を維持する。既存チャンネルの基本設定から変更する場合も、同じ制作構成の項目を使う。

サムネ用文字は `thumbnail_composition.title`、任意の見出しは `headline` と `headline_always_visible: true` で設定できる。文字のない原画像から開始できるよう、プリセットの文字は空にしている。画像エディタで保存した背景・サムネが揃っていれば、自動合成で上書きしない。再合成時は `APP_IMAGE_COMPOSITE_FORCE=1` を指定し、旧画像は `.image_previous/` に退避する。Canvasの任意レイアウトをこの自動合成が読み込むわけではない。手動編集後の書き出し画像を後続工程へ渡す。

```bash
cd Python
python3 app_pipeline.py <N> --channel-folder /absolute/channel/folder --dry-run
python3 app_pipeline.py <N> --channel-folder /absolute/channel/folder --only psd_composite
APP_PIPELINE_STEPS=export,qa,meta,localization,thumbnail python3 app_pipeline.py <N> --channel-folder /absolute/channel/folder --duration <動画の秒数>
```

工程キー `psd_composite` / `premiere` は既存の台帳・再開契約を保つため残している。`ffmpeg_only` の内部処理はPillow画像合成とFFmpegへの引き継ぎで、Adobeの起動・テンプレコピー・Premiere preflight・AMEキューを使わない。古い `export_engine: "ame"` が残っても、この制作構成ではFFmpegが優先される。

動画全体の長さはSUNOの1曲240秒とは別。動画ごとに `--duration` またはチャンネルの `default_duration_sec` を決める。現行の未指定時の既定値は10800秒だが、今回の新チャンネルの動画尺として承認された値ではない。

上の書き出し例はアップロードを含めない。`--from export` だけの指定では後続の `upload` まで走るので、公開前の制作には工程を明示する。

## 現在の10曲バッチとの接続

バッチ `6d0696304171342cf6b3` は `output/suno-groove-ten-20260929/` にある単独の楽曲バッチ。レビュー画面の後処理ボタンは採用音源の処理までで、画像・動画・公開まで開始するボタンではない。正式なチャンネルと動画フォルダを決め、ハートで確定した音源・選択ログをその制作に接続してから後続工程を実行する。未選択のテイクを機械的に採用しない。

## 確認した範囲

- 専用の自動テスト：既存チャンネルとの分離、古いAdobeテンプレの非コピー、FFmpegの強制、画像原本の保持、手動画像の再利用、再合成の退避、失敗時の画像復元。
- 実ファイル：1920×1080の背景・サムネ、2曲16秒のH.264/AAC動画、字幕、曲名表示、タイムスタンプ。PSD／Premiereプロジェクトは作成していない。
- 実チャンネルの長時間書き出しとYouTube公開は未実行。生成済みの本番曲と既存チャンネル設定はこの確認に使用していない。

Fable、Claude、Codex、その他の担当者は、音楽の判断根拠を `docs/music/beat-soul-recipe.md`、曲ごとの入力を `docs/music/beat-soul-inputs.md`、制作工程をこの文書で確認する。
