# Wobble Day: Mac Chromeで楽曲生成から保存検証まで

Wobble Day / WobbleDay の楽曲依頼で使う、Automation Studio内の専用入口。他チャンネルへ適用しない。

1. [制作仕様](../docs/music/wobble-day-production.md)を読み、Notion正本の最新決定・採用画像を取得する。取得できなければ親へ必要部分だけ求める。
2. 依頼・既存会話・親のpacketから、気分、タイトル方針、希望尺、**音源数と生成回数の区別**、既存画像reference、保存先を解決する。気分未指定時だけ「穏やかに揺れる／軽快に弾む、どちら？」。指定済み項目の再質問は不要。新規音源数がなければCreateしない。
3. [受渡しテンプレート](../config/music_requests/wobble-day-request.template.json)をrunごとの`request.json`へ具体化する。制作仕様の実行ゲートを確認し、`documentation_only`、許可false、数の不一致、画像未選択なら生成準備に進まない。新画像・動画作成、公開、契約、旧cloud20再開を楽曲依頼に含めない。
4. 制作仕様の`wobble_suno_bridge.py hold-lock`でforegroundの排他を保持し、Macの正式Chrome CUAで対象Workspace・曲の実在・現在UI・クレジットを読む。`reserve`で原稿のタイトルを予約し、Instrumental・タイトル・Styles・尺・モデル・全生成設定を送信前に読み戻す。
5. 指定数分のみCreateし、各回の受理結果・曲IDを記録。受理不明なら照合を先に行い、Createを再送しない。生成完了を待ち、対象IDだけを同じChrome経路でダウンロードする。
6. `download-manifest.json`を作り、`python3 Python/scripts/verify_suno_downloads.py <manifest> --output <新しいreport.json>`で実ファイルを確認する。数・ID対応・実尺・hash・全decodeの検証と、冒頭の気分・歌なしの聴感確認を分けて報告する。

これは自然言語からCUAを使うエージェント手順であり、`studio.py`の新しい自動実行サブコマンドではない。既存`studio.py suno-auto`、queue、Playwright/headless/旧bot経路へ自動で委譲しない。正式Chrome接続が使えない場合は、その段階の成果物と停止理由を返す。

今回の導入は仕様・受渡し・保存検証のみ。クレジット消費を伴うWobbleインストの通し実行はまだ検証していない。
