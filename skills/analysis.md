# analysis: 競合・seed・プロンプト分析ドメイン

## 目的
YouTube ベンチマークデータ、seed 動画、コメント・サムネ分析から music_direction / visual_direction / metadata prompt へ渡す運用知見を作る。

## 入口コマンド
- 競合分析: `python3 Python/studio.py analyze --dry-run`
- seed 動画: `python3 Python/studio.py seed-analyze --url <YouTube URL> --dry-run`
- seed 音源: `python3 Python/studio.py seed-audio --url <YouTube URL> --dry-run`

## 前提リソース
- YouTube API key または登録済み benchmark cache
- Gemini API key（seed 音源の実聴分析）
- Claude/Codex CLI（分析 JSON と調停）
- yt-dlp / librosa（ローカル音源分析時）

## 並列可否
- benchmark / seed 系は global 1 を基本にする。
- LLM と YouTube quota を共有するため、同時に複数分析を走らせない。

## 典型手順
1. `/api/benchmark/channels` でベンチチャンネルを登録・キャッシュ化。
2. `app_competitor.py --analyze` で `competitor_analysis_cache.json` を更新。
3. seed 分析は outlier 指標をコードで計算し、LLM には用途・仮説・安全性整理を任せる。
4. downstream では `seed_prompt_hint()` と `seed_music_profile_hint()` が meta / SUNO prompt に注入される。

## 失敗時の対処
- API key 不足: dashboard config / env / key file を確認。
- JSON 抽出失敗: プロンプト変更後は実走 1 回だけで確認。
- Gemini 失敗: DSP ローカル分析または既存 seed analysis で続行可。
