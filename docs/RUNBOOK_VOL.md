# Vol Production Runbook

Automation Studio で 1 本の vol を作る一気通貫手順。短い指示だけで開始した場合も、先に `AGENTS.md`、該当 `skills/<domain>.md`、`docs/ARCHITECTURE_MAP.md` を確認してから、このチェックリストで実行する。

## 0. 実行前チェック

- [ ] 作業場所が `_claude` ルートであることを確認する。
- [ ] `python3 Python/studio.py <intent> --vol <N> --dry-run` で解決結果と実コマンドを確認する。
- [ ] 既存 SUNO 実行を確認する。

```bash
pgrep -f suno_auto_create || true
```

- [ ] Web サーバーが port 8888 で応答することを確認する。

```bash
curl -fsS http://localhost:8888/api/config/migration-status >/dev/null
```

失敗時:
- SUNO 実行中なら、同時に SUNO を動かさず完了を待つ。
- port 8888 が落ちていれば、launchd 常駐時は `bash setup_launchd.sh --sync`、非常駐時は `bash Python/start.sh`。
- ネットワーク断や接続拒否は即中断せず、`curl` が 200 を返すまで待って再試行する。

## 1. Vol フォルダ作成

- [ ] 公開日を決める。フォルダ日付は `publish_date` と一致させる。
- [ ] 作成前に dry-run で route を確認する。

```bash
python3 Python/studio.py create --publish-date YYYY-MM-DD --dry-run
```

- [ ] 作成する。

```bash
curl -s -X POST http://localhost:8888/api/videos/create \
  -H 'Content-Type: application/json' \
  -d '{"publish_date":"YYYY-MM-DD"}'
```

- [ ] 予約公開時刻は「フォルダ日付 + `channel.publish_time_jst`」。チャンネル設定で現在値を確認する。

失敗時:
- 既にフォルダがある場合は、`curl -s http://localhost:8888/api/videos` で該当 `video_name` を確認して再利用する。
- API が応答しない場合は 0 のサーバー確認へ戻る。
- active channel が違う場合は、`python3 Python/studio.py ... --channel <id> --switch --dry-run` で切替の影響を確認してから実行する。

## 2. テンプレ確認

- [ ] 作成された vol フォルダ名を確認する。

```bash
curl -s http://localhost:8888/api/videos | python3 -c '
import json,sys
for v in json.load(sys.stdin).get("videos",[]):
    print(v.get("num"), v.get("name"), v.get("folder"))
'
```

- [ ] プロジェクトテンプレートが `rw_base.*` から `rw_volNN.*` にコピーされているか確認する。
- [ ] Premiere / Photoshop 用ファイルが足りない場合は、作成 API の結果と channel template 設定を確認する。

失敗時:
- `rw_volNN.*` が無い場合、同チャンネルの直近成功 vol と `rw_base.*` の存在を確認する。
- テンプレ名がチャンネル固有で違う場合は、`.app_channel_config.json` と `docs/CHANNELS/<channel>.md` を優先する。

## 3. 楽曲生成から後処理

- [ ] SUNO は単一リソースなので必ず guard 経由で実行する。
- [ ] dry-run で解決結果を確認する。

```bash
python3 Python/studio.py suno-auto --vol N --count 15 --dry-run
```

- [ ] 実行する。生成、レンダ待ち、ダウンロード、後処理まで自動で進む。

```bash
python3 Python/parallel_guard.py suno-auto -- \
  python3 Python/studio.py suno-auto --vol N --count 15
```

Ragtime Whiskers では `--mode instrumental_filler` が route 側で使われ、Lyrics=Write タブに `[instrumental]` 5000 字充填、Styles=プロンプトで入力される。

失敗時:
- SUNO 接続拒否やネットワーク断: ブラウザやネットワークの復帰を待ち、200 応答確認後に再試行する。
- ログイン要求: Playwright ブラウザで手動ログインしてから再実行する。
- ワークスペース重複や途中 DL 失敗: 実際の workspace id / workspace 名を確認し、`suno_auto_create.py --download-workspace <wid_or_name> --download-dir <vol_folder>` で DL だけ再実行する。
- audio_url cache miss: Playwright コンテキストを閉じ、download-only を再実行する。
- ffmpeg エラー: `ffmpeg` の存在確認後、`python3 Python/app_process_tracks.py <vol_folder>` を再実行する。

## 4. 背景画像

- [ ] 現状の bgimage step は写真調プロンプトを組むバグがあるため使わない。
- [ ] `Python/codex_imagegen.py` を直接使い、チャンネル固有のサムネ文法を厳守して `volN.png` を作る。
- [ ] Ragtime Whiskers の場合は `docs/CHANNELS/ragtime_whiskers.md` を必ず確認する。

例:

```bash
python3 Python/codex_imagegen.py \
  --output-dir "<vol_folder>" \
  --prompt "<channel thumbnail grammar compliant prompt>::volN.png" \
  --quality high \
  --output-format png \
  --n 1 \
  --reference-image "<reference1>" \
  --reference-image "<reference2>" \
  --reference-image "<reference3>"
```

失敗時:
- 参照画像が無い場合は、チャンネルの Picked / rival thumbs / pose sheet を探す。
- 生成画像に文字やロゴが焼き込まれた場合は破棄し、プロンプトに readable text / logo / watermark 禁止を明記して再生成する。
- チャンネル識別子が欠けた場合は、チャンネルルールを満たしていないので再生成する。
- `codex_imagegen.py` が timeout した場合は API / Codex CLI の状態を確認し、必要なら timeout を延長して再実行する。

## 5. PSD 合成

- [ ] Photoshop は単一リソースなので必ず guard 経由で実行する。
- [ ] 文字レイヤーのフォントは変更しない。
- [ ] dry-run で解決結果を確認する。

```bash
python3 Python/studio.py psd --vol N --dry-run
```

- [ ] PSD 合成を実行する。

```bash
python3 Python/parallel_guard.py psd -- \
  python3 Python/app_pipeline.py N --only psd_composite
```

- [ ] `volN.jpg` と `サムネイル.jpg` が出ていることを確認する。

失敗時:
- Photoshop 接続不可: Photoshop 起動、UXP/CEP パネル、`cep_extension/install.sh` を確認する。
- 背景画像が見つからない: 4 に戻り `volN.png` を作る。
- フォント差し替えが発生した場合は、成果物を採用せずテンプレ PSD と文字レイヤー設定を確認する。

## 6. Premiere 以降のパイプライン

- [ ] Premiere / AME は単一リソースなので guard 経由で実行する。
- [ ] dry-run で解決結果を確認する。

```bash
python3 Python/studio.py pipeline --vol N --from premiere --dry-run
```

- [ ] Premiere 以降を実行する。ffmpeg エンジン設定の場合は ffmpeg でレンダし、QA、メタ、localization、予約アップロードまで進む。

```bash
python3 Python/parallel_guard.py premiere -- \
  python3 Python/app_pipeline.py N --from premiere
```

失敗時:
- Premiere preflight で止まる: `export_engine=ffmpeg` か Premiere 実機利用かを channel 設定で確認する。
- MP4 が無い: export step のログを確認し、`python3 Python/app_pipeline.py N --from export` で再開する。
- メタ生成で quota / LLM エラー: `python3 Python/app_pipeline.py N --from meta` で再開する。
- YouTube OAuth エラー: `python3 Python/app_youtube.py --auth-only` または channel auth の手順で再認証する。トークンや secret はドキュメントに保存しない。

## 7. 最終検証

- [ ] MP4 を `ffprobe` で実確認する。

```bash
ffprobe -v error -show_entries format=duration \
  -show_streams "<vol_folder>/<mp4_name>" | sed -n '1,120p'
```

- [ ] 尺が 3 時間想定、映像・音声 stream、codec、解像度が妥当であることを確認する。
- [ ] `サムネイル.jpg` を 320px 幅で確認し、決めゼリフと猫の目が読めることを確認する。
- [ ] タイトルがチャンネルの公式に合っていることを確認する。
- [ ] `youtube_upload.json` で `publishAt` / `scheduled_at_jst` が「フォルダ日付 + `channel.publish_time_jst`」になっていることを確認する。

失敗時:
- ffprobe で尺や codec が不正: `python3 Python/app_pipeline.py N --from export`。
- サムネが読めない: 4 または 5 に戻る。
- タイトル公式に合わない: meta step を再実行し、サムネ決めゼリフと一致させる。
- publishAt がズレた: upload 設定と channel `publish_time_jst` を確認し、再アップロードや YouTube snippet 更新の前に dry-run する。
