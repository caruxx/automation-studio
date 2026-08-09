# VPS分割計画（マルチユーザー化）

> **リポジトリ分割 (2026-08-09)**: VPS 側の実装は
> [caruxx/automation-studio-vps](https://github.com/caruxx/automation-studio-vps)
> へ分離した(履歴ごと subtree split)。このリポジトリは Mac 側
> (制作パイプライン+ワーカー運用)のみを扱う。本書は分割の経緯と
> カットオーバー条件の記録として残す。

作成: 2026-08-09 / 承認済み決定事項に基づく実施計画。

## 決定事項（2026-08-09 ユーザー承認）

1. **VPS = 制御プレーン**: ユーザー管理・チャンネル管理・ジョブキュー・スケジューラ・分析・UI。
2. **Mac = 実行プレーン**: 素材・音源(SUNO)・Adobe工程・ffmpeg書き出し・YouTubeアップロード。
3. **refresh_token は VPS 保管**（DB暗号化カラム）。ジョブ払い出し時にワーカーへ渡す。
4. VPS は Hetzner。ffmpeg を Mac に残すため高性能プランは不要（CPX21〜31想定）。
5. セキュリティ実装は ec-research-app（DEV/ec-research-app/backend）を正とし移植する。

## アーキテクチャ

```
ユーザー(ブラウザ) --HTTPS--> Nginx --> vps/ FastAPI + Postgres (Hetzner)
                                          |  ジョブキュー / users / channels / worker_tokens
Mac ワーカー(各ユーザー) --HTTPS pull--> /api/worker/*   (Mac側から一方向。VPSからMacへは接続しない)
Mac --SSH--> VPS ライブ配信 (既存 app_live.py 系統。本計画では触らない)
```

- 新規コードは `vps/` サブプロジェクトに置く（既存 Python/app.py の Mac 用モノリスとは分離。
  既存アプリは段階的にワーカー化する）。
- ワーカー通信は Mac→VPS の HTTPS pull のみ。long-poll または 30 秒間隔。

## EC からの移植対応表

| 移植元 (ec-research-app/backend/app) | 用途 |
|---|---|
| models/db_models.py の User / UserInvitation / DeviceToken | ユーザー・招待・(型を借りて) WorkerToken |
| security.py | JWT HS256 / bcrypt / SECRET_KEY 検査 / TOTP 前提 |
| dependencies.py | httpOnly cookie + Bearer、get_current_account → get_current_channel に読み替え |
| rate_limit.py | ログイン総当たり対策（Nginx login_zone と二段） |
| services/pii_crypto.py | AES-256-GCM エンベロープ暗号化（トークン保存用） |
| password_policy.py | パスワードポリシー |
| docker-compose.yml / DEPLOY_VPS.md | postgres+backend 構成、Nginx+Let's Encrypt+ufw 手順 |

## フェーズ

1. **土台移植**: vps/ に FastAPI + Postgres + alembic。User / UserInvitation / 認証 / rate limit /
   ログインAPI / ユーザー管理API。Docker で起動、pytest 同梱。
2. **チャンネル＝アカウント化**: YouTubeChannel テーブル + get_current_channel (X-Channel-Id、
   accessible_channel_ids fail-closed、contextvar)。グローバル app_id 依存を VPS 側に持ち込まない。
3. **OAuth 再同意と認証情報の登録**: OAuth は run_local_server を廃し Web redirect_uri フロー
   （ユーザーのブラウザで同意 → VPS が受ける）。取得した refresh_token を DB の暗号化カラムへ保存。
   **既存の .youtube_token.json は import しない**。refresh_token は発行元の client_id に紐づくため、
   別クライアントでは使えない。VPS 側は新規の「ウェブアプリケーション」型クライアントで
   チャンネルごとに再同意を取る（既存はデスクトップ型でループバック redirect のため流用不可）。
   GCP プロジェクトは既存と同一（2026-08-09 ユーザー決定）。videos.insert は 100 ユニット
   （2025-12 に 1600 から引き下げ）、10,000/日 で約 100 本相当のため並行稼働中も枠は逼迫しない。
4. **ジョブキュー + WorkerToken**: ジョブ発行/占有(lease)/完了報告 API。完了報告に YouTube quota
   消費ユニットを同梱し VPS 側で集計。期限接近かつ未着手ジョブは Discord 通知。
   排他は DB のジョブ状態で行い、/tmp flock は単一 Mac 内の資源(Adobe/SUNO)専用に格下げ。
5. **Mac ワーカー実装とカットオーバー**: 既存 Python/ のパイプラインをワーカークライアントから
   呼ぶ。書き出し→アップロード→ローカル成果物の整理まで。ここで初めて Python/ に手を入れ、
   正本を VPS へ切り替える。

## 並行稼働とカットオーバー（2026-08-09 ユーザー決定）

現行の localhost 運用を止めずに VPS 側を完成させ、**完成後に一度だけ切り替える**。
移行期間中の鉄則は「**双方向同期を作らない**」こと。正本の向きを常に一方向に固定する。

| 期間 | 正本 | VPS の位置づけ | Mac の位置づけ |
|---|---|---|---|
| フェーズ2〜4（構築中） | Mac の channels.json / .youtube_token.json | 読み取り専用のミラー。取り込みは Mac→VPS の一方向 import のみ | 従来どおり localhost で通常運用。コードは変更しない |
| カットオーバー当日 | 切替 | ここで正本になる | ワーカーモードへ切替 |
| 以降 | VPS の DB | 正本 | ワーカー専用 |

### 移行期間中の禁止事項

- 既存 `Python/` のコードをフェーズ2〜4で変更しない（変更はフェーズ5のカットオーバー時のみ）。
- VPS 側から channels.json / .youtube_token.json へ書き戻さない（import は読むだけ）。
- **既存の Google OAuth クライアントを削除・変更しない**。過去に旧クライアント削除で
  YouTube Analytics のトークンが一括失効した事故がある。VPS 用は新規クライアントを別途作る。

### OAuth を分離する理由

VPS には専用の Google Cloud OAuth クライアントを新規作成し、VPS 側で改めて同意を取る。
これにより並行稼働中も Mac と VPS が認証情報を共有せず、片方の事故がもう片方に波及しない。
quota も別枠になるため、移行期間中に既存運用の枠を圧迫しない。

### カットオーバー条件（全て満たすまで切り替えない）

1. VPS 上で全チャンネルの認証が通り、テスト動画のアップロードが成功する。
2. Mac ワーカーがジョブを取得し、書き出しからアップロードまで完走する。
3. 予約投稿が publish_time_jst どおりに反映される。
4. ロールバック手順が文書化され、旧経路が即座に復帰できる状態にある。
5. **Postgres 上でジョブの二重取得が起きないことを実機で確認する**。ローカルには Docker も
   postgres も無いためテストは SQLite でしか回っておらず、本番の `FOR UPDATE SKIP LOCKED`
   経路は未検証。VPS 構築後に複数ワーカーから同時 lease をかけて確かめること。

## セキュリティ要点

- 本番 APP_ENV=production で SECRET_KEY 32 文字未満は起動拒否（EC と同じ）。
- refresh_token / client_secret は平文で共有ドライブに置かない。DB 暗号化カラムのみ。
- 自己サインアップは開けない。UserInvitation の招待制のみ。
- WorkerToken は user_id 紐付け・ハッシュ保存・disabled_at で失効・last_seen_at で棚卸し。
- Docker は 127.0.0.1 バインド + Nginx 経由のみ公開。ufw + Hetzner Cloud Firewall 併用。
- backend コンテナは mem_limit で閉じ込め（EC の OOM 事故 22 回の教訓）。

## 本計画で触らないもの

- 既存 Python/app.py（Mac 用）と app_live.py の SSH 橋渡し。
- SUNO / Adobe / Drive 素材まわりの Mac 依存実装。
