# Automation Studio / ライブ受け取り環境の復旧記録

2026-09-06。Claude の Automation Studio セッションから引き継いで実施。

## Studio

- 移行先: Xserver、SSH alias `xserver`、`/opt/automation-studio-vps`。
- 既存 backend（localhost:8001）と PostgreSQL を利用。EC のサービス・DB は変更していない。
- `/etc/nginx/sites-available/automation-studio` を追加し、`studio.caruvistar.jp` を backend へ接続。既存 Cloudflare origin 証明書を利用。
- nginx 設定検証成功、公開ログイン画面 HTTP 200、未認証 `/api/channels` HTTP 401、既存 EC health HTTP 200 を確認。
- 暗号鍵が欠落していたため新規生成。`/etc/automation-studio/keyring.json`、所有者 10001:10001、mode 0600。鍵の値はこの記録・Git に保存しない。
- 空の DB にチャンネル定義 9 件を投入。名前・フォルダ対応のみで、旧 OAuth トークンは復旧していない。
- 管理者は未作成。利用するメールアドレスの回答待ち。管理者作成後に MFA 設定、YouTube OAuth の再同意、Mac worker の再接続が必要。
- 新しい暗号鍵の別媒体バックアップは未実施。旧 Hetzner の DB / 暗号鍵は回収できていない。

## ライブ受け取り環境

- `/opt/ytlive/{bin,scripts,channels,videos,logs,status,licenses}` を準備。
- BtbN の FFmpeg 9 系 Linux GPL build を公式ダウンロード案内から取得し、配布 SHA-256 と照合。ffmpeg / ffprobe を専用 bin に配置。RTMP / RTMPS 対応を確認。
- `stream.sh`、`ytlive.sh`、`status.py` を scripts に配置。専用 bin を参照する PATH 対応をローカルソースにも追加。
- status は `ok=true`、`ffmpeg_installed=true`。5 配信ともキー設定あり・停止中・動画未配置を確認。
- 動画転送と配信開始は実施していない。
- 転送先を示した確認に対しユーザーから保存承認を得て、既存 5 配信（orzz / orzz_2 / orzz_3 / sukima / rw_1）の設定を `/opt/ytlive/channels/` に保存済み。SSH 標準入力で転送し、保存後のファイル内容の SHA-256 一致、root:root / 0600、親ディレクトリ 0700 を確認。キーの値は画面・ログ・Git に記録していない。
- ローカル `config/live_config.json` の接続先を Xserver（210.131.213.137）、鍵を `~/.ssh/xserver_vps.pem` に更新し、読み戻しで確認。既存の配信キー・動画指定・その他の配信設定は維持。動画の受け取り先 `videos/orzz` / `videos/sukima` / `videos/rw` も準備済み。

## SUNO

- 既存 CLI に復号取得を統合。`APP_SUNO_DL_MODE=legacy` で旧分岐を選択可能。
- orzz_vol158 を 20 曲取得し MP3 / stereo / 約 292〜473 秒を確認。独立した 2 回の取得で、各 song ID に対応する復号バイトの SHA-256 が一致。
- 既存 CLI からも 20/20 成功。既存 `app_process_tracks.py --keep-names --no-dedup` の後処理も 20/20 成功、原本バックアップ 20 件と加工後 20 件を確認。
- legacy は実ソースの分岐テストで確認。旧方式による実ダウンロード成功は未検証。
- 大 Workspace はカード 982 曲・走査時に検出した総数 915 曲の不一致で停止。実 UI で Hide Disliked / Hide Stems / Hide Clips from Edit Mode の 3 フィルターが選択されていることを確認した。
- 全曲列挙の前に選択済みフィルターを解除し、件数が 3→2→1→0 と減ることを確認する処理を追加。実 UI で解除完了と再生イベント 0 を確認。不明な状態では推測して解除せずエラーにする。
- 解除後の 982 曲全件列挙・完全取得は未検証。915 の数値が出た直接の原因も未確定であり、20 曲の検証結果とは区別する。
- 検証音声は `/tmp/suno_fast_vol` と `/tmp/suno_existing_flow`。チャンネル素材へ投入せず、動画制作・アップロードは行っていない。
