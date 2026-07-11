# Ragtime Whiskers Channel Rules

この文書はユーザー確定事項。変更しない。

## ブランド

- 1900-1920s Jazz Age × ベル・エポック石版画調。
- 黒猫を必ず入れる。
- 黒猫の識別子は、琥珀の目、クリーム色ネッカチーフ、金の三日月チャーム。
- ポーズシート: `素材/dark_old_jazz_cat_pose_sheet_transparent.png`

## SUNO 確定方式

- Lyrics は Write タブに `[instrumental]` × 5000 字充填。
- 実行モードは `--mode instrumental_filler`。
- Styles はプロンプトを入れる。
- ベースプロンプトは `channel.suno.prompt`。
- ベースプロンプトの要件は、62〜68bpm、サックス + アコピ主導、禁止事項付き。
- 動画タイプ別変種は `.app_channel_config.json` の `suno.prompt_variants`。
- `night` は 68bpm。
- `morning` は 62bpm。

## サムネ確定文法

- ベンチマークである Sepia Lounge の情景文法を使う。
- 情景文法は、ソファ / ラウンジ + 小道具 + ランプ + オーカー中間トーンの壁。
- 上記の情景文法に、うちの猫を掛け合わせる。
- ポスター調、つまり情景なしの構図は NG。
- 三日月はチャーム + もう 1 箇所に入れる。
- 猫は画面高 60-70%。
- 320px で目が読めること。
- ベンチマークの文言丸写しは禁止。
- 例として `"No Rush. Just Jazz."` は使わない。

## タイトル公式

- 公式: 「短いフレーズ. – Slow, Smoky/Cozy + Vintage Noir Jazz + フック」
- サムネの決めゼリフと一致させる。

## 投稿

- 3 時間尺。
- 公開はフォルダ日付 + `publish_time_jst`。
- 現在の `publish_time_jst` は 02:00。
- ベンチマーク的最適は土曜 23 時 JST。

## ベンチマーク

- Sepia Lounge
- Hyperbolic Club
- Pause,maybe?
- `rival_channels` 設定済み。
