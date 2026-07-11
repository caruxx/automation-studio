# かんたん動画編集（ffmpeg 補助）

元ファイルを変更せず、同じフォルダに `<元名>_edited_<連番>` で出力する軽量編集機能です。

## Web UI

編集タイムラインと役割が重複するため、「書き出し後の仕上げ」は Web UI から撤去済みです。この機能は AI / CLI 向けとして維持し、下記の `studio.py video-edit` または `/api/video-edit/*` から実行します。

## 対応操作

- `trim`: 開始/終了秒でカット。通常は stream copy、精密カットのみ再エンコード。
- `concat`: 複数ファイル結合。codec 構成が同じなら copy。
- `loop_to_duration`: 指定尺までループし、末尾を映像/音声フェード。
- `fade`, `replace_audio`, `burn_overlay`, `extract_frame`, `convert`

## CLI

```bash
python3 Python/studio.py video-edit --operation trim \
  --input "/absolute/path/input.mp4" \
  --params '{"input_path":"/absolute/path/input.mp4","start":5,"end":20}' --dry-run
```

実行時は `--dry-run` を外します。`app_video_edit.py` は `resource_lock('ffmpeg_edit')` を取得し、編集同士を直列化します。ffmpeg/ffprobe には watchdog/timeout があり、ffmpeg は `nice -n 10` で実行します。

## API

- `POST /api/video-edit/run`
- `GET /api/video-edit/status?job_id=...`
- `GET /api/video-edit/history`

vol の `video_name` を付けた場合、入力と補助素材はその vol 配下だけ許可されます。任意ファイルは `video_name` を空にし、絶対パスで指定します。
