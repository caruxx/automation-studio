# app-rename-audio: 楽曲リネーム + 音声処理

SUNOからダウンロードしたMP3ファイルを、サムネイル画像の雰囲気に合った英語タイトルにリネームし、
フェードアウト処理を自動適用するスキル。

## 概要
1. サムネイル画像をClaude CLIで画像認識 → 英語タイトルを曲数分生成
2. MP3ファイルをタイトルでリネーム（z_プレフィックス保持）
3. musicフォルダ未存在なら FFmpeg で末尾トリム + 8秒フェードアウト

## 実行方法

### Automator Quick Action から
フォルダを右クリック → クイックアクション → rename_music を実行

### Claude Code / ターミナルから
```bash
# ラッパースクリプト経由（base64デコード→一時ファイル→Terminal実行）
bash /path/to/wrapper.sh "/path/to/67_vol_260405"
```

## 処理詳細

## Web Studio 統合版 (`app_process_tracks.py`)

Web ダッシュボード「楽曲」タブの 2 ボタンから呼ばれる Python 実装:
- スクリプト: [Python/app_process_tracks.py](../Python/app_process_tracks.py)
- API: `POST /api/videos/{name}/process-tracks[?rename_only=true]`
- **いいね数（`z+_` プレフィックス）は両モードで保持される**
  - 例: `zz_song.mp3`（いいね 2）→ タイトル提案後 `zz_New_Title.mp3`

### タイトル提案の優先順位

| 順序 | 情報源 | 関数 |
|------|--------|------|
| 1 | サムネ画像（`vol*.jpg` / `サムネイル.jpg` / `vol*.png`） | `propose_titles_from_thumbnail()`（`claude -p --allowedTools Read`） |
| 2 | **チャンネルペルソナ**（`~/.config/{app_id}/dashboard_config.json` の `persona`） | `propose_titles_from_persona()` |
| 3 | どちらも無い | 既存ファイル名を保持 |

サムネが存在しない（動画初期）や、サムネだけで世界観が伝わらない場合のフォールバックとして、
チャンネル全体のコンセプト（persona）を Claude CLI に渡して
その世界観にマッチする英語タイトルを N 個生成する。

### 2 つのモード

| モード | ボタン | 挙動 |
|--------|------|------|
| **リネームのみ** | ✏️ タイトルのみリネーム | root 直下で rename のみ、ffmpeg スキップ、数秒〜十数秒 |
| **フル後処理** | 🎛 後処理を実行 | `original_music/` バックアップ + ffmpeg 加工 + `music/` 出力 + root リネーム |

### FFmpeg 処理（フル後処理のみ）

```
silenceremove=stop_periods=-1:stop_duration=0.2:stop_threshold=-80dB
→ afade=t=out:st=(duration-8):d=8
→ loudnorm I=-16:TP=-1.5:LRA=11
-c:a libmp3lame -b:a 192k
```

YouTube BGM の標準ラウドネス（-16 LUFS）に正規化、末尾 8 秒フェードアウト、
末尾の無音 0.2秒以上を除去。

### CLI

```bash
# リネームのみ（ffmpeg スキップ）
python3 app_process_tracks.py /path/to/77_vol_260416 --rename-only

# フル後処理（従来どおり）
python3 app_process_tracks.py /path/to/77_vol_260416

# ドライラン（プレビューのみ）
python3 app_process_tracks.py /path/to/77_vol_260416 --dry-run
```

## Automator/Shell 版 (既存のクイックアクション)

### Phase 1: リネーム
- サムネイル検出: `vol*.jpg` → `サムネイル.jpg` の優先順
- Claude CLI 呼び出し（**API未使用・JSON出力**）:
  ```bash
  claude -p "画像ファイル '<path>' を Read ツールで読み取り、
  雰囲気に合う英語タイトルを <N>個 提案してください。
  出力は単一のJSONオブジェクトのみ:
  {\"titles\":[\"Title One\",\"Title Two\", ...]}
  前後の説明文・コードフェンス不要。" --allowedTools Read
  ```
- 出力パース: レスポンスから `{...}` を抽出 → `titles[]` を採用
- タイトル生成条件: 英語、1タイトル1要素、番号・記号不要
- z_プレフィックス保持: いいね曲の `z_` は維持してリネーム
- プレビューテーブル表示 → y/N 確認 → 実行
- **SUNO 生成との統一方式**: `Python/suno_auto_create.py` の Claude プロバイダーと
  同じ JSON 単一オブジェクト契約を踏襲。リトライ時も毎回新規プロセスで再提案（考案）→ 採用。

### Phase 2: FFmpeg 音声処理
musicフォルダが存在しない場合のみ実行:
1. `original_music/` にオリジナルMP3をバックアップ
2. 末尾無音トリム: `silenceremove` フィルター（-80dB閾値、0.2s持続）
3. 8秒フェードアウト: `afade=t=out:st={duration-8}:d=8`
4. `music/` フォルダに処理済みファイル出力

### FFmpeg設定値
```
FADE_SECONDS=8
SILENCE_DURATION=0.2
SILENCE_THRESHOLD="-80dB"
TIMEOUT_SEC=1800
```

### FFmpeg 自動インストール
ffmpeg未検出時: Homebrew自動インストール → brew install ffmpeg

## 前提条件
- Claude CLI がインストール済み（リネーム用の画像認識）
- サムネイル画像が動画フォルダ内に存在すること
- MP3ファイルが動画フォルダ直下に存在すること
