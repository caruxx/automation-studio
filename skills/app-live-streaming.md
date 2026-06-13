# app-live-streaming: VPS 経由 YouTube 24/7 マルチライブ配信

VPS（Ubuntu/Debian）上で ffmpeg + screen を使い、作成済み BGM 動画を **複数チャンネル × 複数配信**で 24時間ループ配信するスキル。
ダッシュボードの「ライブ配信」タブから初期設定・稼働モニタリング・動画差し替え・ローテーション・自動打切り・視聴者数・サムネ/タイトル編集まで一括管理する。
**配布想定**: VPS 接続情報・ストリームキーは各ユーザーのローカル設定（`~/.config/{app_id}/live_config.json`、chmod 600）に保存され、リポジトリには含まれない。

## データモデル（v2: グループ＝チャンネル、その下に複数配信）

```
グループ "orzz"（= YouTube チャンネル orzz. / プレフィックス）
├── 動画プール: VPS /opt/ytlive/videos/orzz/*.mp4   ← グループ内の全配信で共有
├── 配信 orzz_1 … screen ytlive_orzz_1 / channels/orzz_1.env / 専用ストリームキー
├── 配信 orzz_2 … （1チャンネルに5本でも10本でも。キーは配信ごとに発行）
└── …
グループ "sk"（= SUKIMA）
└── 配信 sk_1 …
```

- **配信ストリーム**ごとの設定: ストリームキー / 単発動画 or プレイリスト / ローテ間隔(h) / 最大配信時間(h) / copy・reencode
- グループは `registry_id` で config/channels.json に紐付き、YouTube トークン・チャンネルID・動画/サムネ候補のフォルダを引く
- 1つの YouTube チャンネルで複数ライブを同時配信するには、YouTube Studio で**配信ごとに「新しいストリームキーを作成」**する（既定キーの使い回し不可）

## 配信エンジン（VPS 側 stream.sh）

- 既定 `mode=copy`: `ffmpeg -y -fflags +genpts -re -stream_loop -1 -i video.mp4 -c copy -f flv rtmp://…/KEY`（再エンコード無し・CPU 数%/配信）
- **自動復旧**: クラッシュ/回線断は 5 秒後に同じ動画で再接続（YouTube は数十秒の断なら同一ライブ継続）
- **プレイリスト巡回**: `PLAYLIST`（コロン区切り）+ `ROTATE_SECONDS` → `timeout` で定時終了(code 124)→ 次の動画で再接続。`status/<id>.idx` が巡回位置
- **自動打切り**: `MAX_SECONDS` 経過で stopfile を作って screen ごと終了（再起動しない）
- **無停止差し替え（swap）**: `ytlive.sh swap <id> [next]` が ffmpeg のみ kill → wrapper が最新 env / idx で数秒後に再接続。**ライブ URL・視聴者・チャットは維持される**（ノボさん方式の「止めずに差し替え」）

## ファイル構成

| 場所 | ファイル | 役割 |
|------|---------|------|
| Mac | `Python/app_live.py` | SSH/scp 制御・設定管理（v1→v2 自動マイグレーション） |
| Mac | `Python/routers/live.py` | API（下表） |
| Mac | `Python/vps_live/{stream.sh,status.py,ytlive.sh}` | VPS へ配置されるスクリプト原本（セットアップ時 push） |
| Mac | `~/.config/{app_id}/live_config.json` | VPS 接続情報 + streams 設定（**stream key 含む・600**） |
| VPS | `/opt/ytlive/channels/<id>.env` | 配信別設定（600。GROUP/PLAYLIST/ROTATE_SECONDS/MAX_SECONDS 含む） |
| VPS | `/opt/ytlive/videos/<group>/*.mp4` | グループ共有の動画プール |
| VPS | `/opt/ytlive/status/<id>.{props,idx,stop}` / `logs/<id>.log` | 稼働状態 / 巡回位置 / 停止フラグ / ffmpeg ログ |

## API

| Method/Path | 役割 |
|---|---|
| GET/PUT `/api/live/config` | 設定取得（key マスク）/ 保存（streams 全置換・VPS env 自動 push・削除分は VPS 後始末） |
| POST `/api/live/setup` | 初期設定一括（鍵生成→鍵登録→ffmpeg/screen→スクリプト配置→env 同期） |
| GET `/api/live/status` | VPS 負荷 + グループ別容量(group_disk) + 配信別稼働（uptime/残り時間/idx/再起動回数） |
| POST `/api/live/streams/{id}/start・stop・restart` | 配信制御 |
| POST `/api/live/streams/{id}/swap?next=1` | **無停止差し替え**（next=1 でプレイリスト次の動画へ） |
| GET `/api/live/streams/{id}/log` | ffmpeg ログ tail |
| GET `/api/live/viewers?force=1` | **同時視聴者数**（liveBroadcasts+liveStreams+videos.list ≈3 quota/グループ・5 分キャッシュ） |
| GET `/api/live/local-videos・local-thumbnails` | ローカル候補（チャンネルフォルダの mp4/mov/m4v・大文字可 / jpg・png）。`folder=` で外部 SSD 等の任意フォルダ or 動画ファイル単体を指定可（直下+1階層・10MB以上・`._*` 除外・`skipped_small` 返却） |
| GET `/api/live/pick-local?kind=folder\|file` | macOS ネイティブ選択ダイアログ（osascript）。サーバー Mac の画面に表示。file は複数可・キャンセルは `status=cancelled` |
| POST `/api/live/upload` → GET `/api/live/upload/{job_id}` | **再開可能アップロード**（tail+ssh append 方式・並行可・自動リトライ。進捗無し8回連続で失敗。同一宛先はジョブ dedup） |
| GET `/api/live/uploads?group=` / DELETE `/api/live/upload/{job_id}` | ジョブ一覧（実行中+6h以内の完了/失敗・ページ遷移後の再アタッチ用・進捗は1SSHでまとめ取得）/ 中止 |
| GET/DELETE `/api/live/remote-videos` | プール一覧（使用中マーク付き）/ 削除（使用中はガード） |
| GET/PUT `/api/live/broadcasts` | ライブのタイトル/説明/公開設定(privacy)/AI開示(contains_synthetic_media)。取得は videos.list で AI開示も補完 |
| POST `/api/live/broadcasts/suggest` | タイトル/説明文の LLM 提案（run_llm 経由・保存はしない・UI 入力欄に反映するだけ） |
| POST `/api/live/thumbnail` | ライブのサムネイル設定（thumbnails.set・50 quota） |

## 画面の使い方（運用フロー）

1. **グループ追加**: レジストリからチャンネル選択 + プレフィックス（例 sk）→ `sk_1` が出来る
2. **＋配信追加**: グループヘッダのボタンで `sk_2, sk_3, …` を量産（設定は #1 をコピー、キーと動画だけ入れ替え）
3. **🎬 動画プール**: ローカル動画をアップロード（進捗%）。使用中マーク・削除・容量表示
4. 各配信の **⚙設定**: ストリームキー / 単発動画 or プレイリスト（プールから追加）/ ローテ間隔 / 最大時間 → 保存
5. **▶開始**。表のセル: 状態・経過/残り・👁視聴者・現在の動画・ローテ位置
6. **差し替え**（伸びが鈍ったら）: 動画 or プレイリスト変更 → 保存 → **⇄差替**（配信は止まらない）
7. **YouTube 配信情報**: タイトル/説明編集 + サムネ画像適用（配信ごと）

## 運用ノウハウ

- **1チャンネル多配信**: ノボさん運用 = 1ch で 10 配信、3-4 日ごとに伸びない配信から動画差し替え（swap）。チャンネルパワーで同時配信数の上限が変わる模様 → 少数から漸増が安全
- **帯域**: 送信 Mbps ≒ Σ(動画ビットレート)。Hetzner 20TB/月 ≒ 平均 60Mbps 上限。4Mbps×9 配信 ≒ 月 11.6TB が現実的ライン
- **CPU**: copy なら 4vCPU で 10-20 配信可。ボトルネックは帯域とディスク
- **ディスク**: 75GB なら動画 1-2GB × 30-40 本程度。プールの容量バッジと「空き GB」を見ながら削除
- **視聴者数の quota**: 約 3 unit/グループ/回・5 分キャッシュ。日次 10,000 の数%で収まる
- **VPS 直接操作**: `screen -ls` / `screen -r ytlive_orzz_1`（デタッチ Ctrl+A→D）/ `bash /opt/ytlive/scripts/ytlive.sh swap orzz_1 next`

## トラブルシュート

| 症状 | 確認・対処 |
|---|---|
| 接続エラー（Permission denied） | 鍵未登録。セットアップでパスワード入力 or 公開鍵を Hetzner Console 等で手動登録 |
| 配信中なのに YouTube に映らない | キー誤り/別配信と重複（ログに I/O error 等）。配信ごとに専用キーを発行して保存→再起動 |
| 「待機/異常」バッジ | key/動画未設定 or 動画パス誤り（ログ参照）。プレイリストのパス typo に注意 |
| ローテが効かない | プレイリスト2本以上 + ローテ間隔>0 + 保存後に再起動（or 差替）したか |
| 視聴者数が「未取得」 | そのチャンネルの `.youtube_token.json` 未認証、またはキーが YouTube 側の boundStream と不一致 |
| copy で映像乱れ | 入力が H.264+AAC でない → reencode で検証 or AME で再書き出し |
| 削除できない動画 | いずれかの配信の動画/プレイリストで使用中 → 設定から外してから削除 |

## 関連

- レジストリ: [app-master-config.md](./app-master-config.md)（config/channels.json）
- YouTube 認証: [app-youtube-upload.md](./app-youtube-upload.md)（チャンネル別 `.youtube_token.json` を流用）
