# VPS分割計画（マルチユーザー化）

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
3. **認証情報の移送**: .youtube_token.json → DB 暗号化カラム。OAuth は run_local_server を廃し
   Web redirect_uri フロー（ユーザーのブラウザで同意 → VPS が受ける）。client_secret はユーザー毎
   登録可（quota 別枠化）。
4. **ジョブキュー + WorkerToken**: ジョブ発行/占有(lease)/完了報告 API。完了報告に YouTube quota
   消費ユニットを同梱し VPS 側で集計。期限接近かつ未着手ジョブは Discord 通知。
   排他は DB のジョブ状態で行い、/tmp flock は単一 Mac 内の資源(Adobe/SUNO)専用に格下げ。
5. **Mac ワーカー実装**: 既存 Python/ のパイプラインをワーカークライアントから呼ぶ。
   書き出し→アップロード→ローカル成果物の整理まで。

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
