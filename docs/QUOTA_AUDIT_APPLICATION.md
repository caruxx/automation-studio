# YouTube API Services — 監査・クォータ拡張申請ドラフト

作成: 2026-07-04 / 対象フォーム: YouTube API Services - Audit and Quota Extension Form
(https://support.google.com/youtube/contact/yt_api_form)

> 提出はユーザー(abe_kota@caruvistar.jp)のGoogleアカウントで行う。本ドラフトの[ ]内は提出前に確認・記入。

---

## 0. 事前チェックリスト(提出前に必須)

- [ ] Google Cloud プロジェクトの OAuth 同意画面に、公開プライバシーポリシーURLを設定(caruvistar.jp に docs/privacy_for_hp.html を掲載したURL)
- [ ] OAuth 同意画面のアプリ名を「Automation Studio」に統一(審査官が見る名前)
- [ ] `/api/quota` メーターで直近の実消費データを7日分以上蓄積(使用実績のエビデンス)
- [ ] デモ動画(3〜5分、画面録画): OAuth同意→アップロード(予約投稿)→統計表示→連携解除までの流れ
- [ ] docs/API_COMPLIANCE.md の内容が実装と一致していることを確認

## 1. アプリケーション概要 (Application Overview)

**日本語**:
Automation Studio は、自社(Caruvi Star / 日本)が運営する複数の音楽(BGM)YouTubeチャンネルの制作・投稿・分析を管理する社内向けデスクトップツールです。動画の予約アップロード、メタデータ(タイトル・説明・タグ・多言語ローカライズ)の設定、自チャンネルの統計確認を行います。一般公開サービスではなく、チャンネル所有者本人が自身のチャンネルに対してのみ使用します。

**English**:
Automation Studio is an internal desktop tool operated by Caruvi Star (Japan) to manage production, scheduled publishing, and analytics for our own music (BGM) YouTube channels. It performs scheduled video uploads, metadata management (title/description/tags/multi-language localization), and statistics review for the authenticated user's own channels only. It is not a public-facing service; each user authenticates their own channels via OAuth.

## 2. API の使用方法 (How the client uses the API)

| Method | 用途 | 1回のコスト |
|---|---|---|
| videos.insert | 完成動画の予約アップロード(private + publishAt) | 100 |
| videos.update | メタデータ更新・多言語ローカライズ | 50 |
| videos.list | 動画情報・処理状態の確認 | 1 |
| videos.batchGetStats | 自チャンネル動画の統計スナップショット(日次) | 1(専用枠) |
| channels.list | 自チャンネルの公開統計(登録者・総再生)日次取得 | 1 |
| thumbnails.set | サムネイル設定 | 50 |

- 認証: OAuth 2.0(チャンネルごとに個別トークン)。スコープは最小限(`youtube.upload`, `youtube`。分析機能有効時のみ `yt-analytics.readonly` を段階的に追加リクエスト)。
- データ保持: API由来データは取得日時を記録し、非統計データは30日以内に削除または再取得。統計データは30日ごとのアクセス権(トークン有効性)確認を記録の上で保持。処理は `data_retention_log.jsonl` に記録。
- 連携解除: ユーザー操作でトークンを revoke エンドポイントにより即時失効し、当該チャンネルのAPI由来データを即時削除。
- 帰属表示: API由来データを表示する全画面に「データ提供: YouTube」と youtube.com へのリンクを表示。

## 3. クォータ内訳と拡張理由 (Quota breakdown & justification)

**現状(既定 10,000 units/日)での想定消費**:

| 項目 | 計算 | units/日 |
|---|---|---|
| アップロード(7チャンネル×1本) | 7 × 100 | 700 |
| メタデータ・ローカライズ更新(1本あたり11言語 = videos.update ×2〜3回) | 21 × 50 | 1,050 |
| サムネイル設定 | 7 × 50 | 350 |
| 状態確認・一覧(videos.list等) | 〜100 × 1 | 100 |
| 日次統計(channels.list) | 7 × 1 | 7 |
| 予備(リトライ・検証) | — | 500 |
| **合計** | | **約2,700** |

**拡張申請の理由(日本語)**:
運営チャンネルを[20]チャンネル規模へ拡大予定であり、1日あたり最大[3]本/チャンネルの予約アップロードと多言語メタデータ更新を行うと、想定消費が約[23,000]ユニット/日となり既定の10,000を超過します。ピーク時の安全率を含め **[50,000] units/日** への引き上げを申請します。

**English**:
We plan to scale to [20] channels with up to [3] scheduled uploads per channel per day. With multi-language metadata updates, estimated consumption reaches ~[23,000] units/day, exceeding the default 10,000. Including retry headroom, we request an increase to **[50,000] units/day**.

## 4. 想定される審査質問と回答

- **Q: データを第三者に提供するか?** A: しない。認可ユーザー本人のチャンネルデータのみを本人に表示。第三者広告なし。
- **Q: 保存期間は?** A: 非統計データは最長30日(自動削除ジョブ)。統計は30日ごとの権限確認付きで保持。ログあり。
- **Q: 複数ユーザーで共有するか?** A: 配布版は各ユーザーが自身のGoogle Cloudプロジェクト(client_secret)を作成して使用するため、本プロジェクトのクォータは自社利用のみ。
- **Q: スクレイピングの併用は?** A: なし。YouTubeデータの取得はすべて公式APIまたは公式エクスポート(Google Sheets経由の手動データ)による。
- **Q: 削除リクエストへの対応は?** A: 連携解除機能でトークン失効+即時データ削除(ポリシーの7日以内を満たす)。

## 5. 提出物リスト

1. フォーム回答(本ドラフト §1〜3)
2. プライバシーポリシー公開URL(caruvistar.jp)
3. デモ動画リンク(限定公開YouTubeまたはDrive)
4. スクリーンショット: OAuth同意画面 / アップロード画面 / 統計画面(帰属表示が写るように) / 連携解除ダイアログ
5. docs/API_COMPLIANCE.md(求められた場合の補足資料)
