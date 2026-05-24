# app-youtube-desc: YouTube 説明文自動生成

動画フォルダ内のタイムコード情報とサムネイル画像から、YouTube動画の説明文を自動生成するスキル。

## 概要
Claude Code が以下の情報を読み取り、チャンネルの雰囲気に合った英語の説明文を生成する:
1. `music_time_code_info_{num}.txt` — タイムコード + 曲名リスト
2. サムネイル画像 (`vol*.jpg` / `サムネイル.jpg`) — 動画の雰囲気
3. 過去の `youtube_description.txt` — 文体・フォーマットの参考

## 実行方法

Claude Code から対象フォルダを指定して実行:

```bash
# 例: 67番の動画フォルダ
claude "67_vol_260405 の youtube_description.txt を生成して"
```

## 生成テンプレート

```
{サムネイルの雰囲気に合った英語の導入文 2-3段落}

---

{タイムコードリスト}
00:00:00 - {曲名1}
00:04:34 - {曲名2}
...

---

#{ジャンルタグ} #{ムードタグ} #{用途タグ}
```

## 生成プロンプト（Claude Codeが内部で使用）

```
以下の情報からYouTube BGM動画の説明文を英語で生成してください。

【サムネイル画像】(添付)
【タイムコード情報】
{music_time_code_info_XX.txt の内容}

【条件】
- 英語で記述
- 導入文は2-3段落、サムネイルの雰囲気に合ったエレガントな文体
- 用途提案を含める（作業用、リラックス、ディナーなど）
- チャンネル登録の呼びかけを含める
- タイムコードリストを "---" で区切って掲載
- 末尾にハッシュタグ10個（#LoungeMusic #ChillVibes など）
- "LOOP" 行はタイムコードリストから除外
```

## 出力先
```
{動画フォルダ}/youtube_description.txt
```

## 参考: 過去の説明文スタイル
```
Elegant music for golden hour moments — the kind of music that plays
when the sun sets over Monaco and everything just feels right...

Hit subscribe if you want more music that matches the mood.
New mixes uploaded regularly.

---

#LoungeMusic #ChillVibes #LuxuryMusic #EveningMusic #RelaxingBGM
#CinematicMusic #WorkFromHome #StudyMusic #SmoothJazz #Monaco
```
