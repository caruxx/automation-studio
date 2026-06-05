#!/usr/bin/env python3
"""
orzz. SUNO 自動楽曲生成スクリプト
Playwright でブラウザを操作し、LLM API で生成したスタイル・タイトルを
SUNO のフォームに自動入力 → Create ボタンをクリック → ループ生成

使い方:
  python3 suno_auto_create.py --prompt "lounge jazz BGM" --count 5
  python3 suno_auto_create.py --config suno_preset.json
"""

import argparse
import json
import os
import re
import sys
import random
import time
from pathlib import Path

# ─── 設定 ──────────────────────────────────────────────

# 設定ディレクトリ（v2 配布化対応・共通モジュール経由）
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
DEFAULT_CONFIG = CONFIG_DIR / "suno_config.json"


class UnattendedLoginRequired(RuntimeError):
    """無人モード (APP_NO_INTERACTIVE=1) で SUNO のブラウザ手動ログインが必要になった場合に raise。
    呼び出し側 (app_pipeline.py) はこれを捕捉して Discord 通知 + 早期失敗で扱う。"""
    pass


class BotChallengeDetected(RuntimeError):
    """SUNO 側で Bot/CAPTCHA チャレンジが表示されたときに raise。
    UnattendedLoginRequired と同じく exit 75 で抜け、pipeline 側で手動介入扱いにする。"""
    pass


def _is_unattended() -> bool:
    """ブロッキング待機を無効化すべきかを判定する。
    優先度: APP_KEEP_BROWSER=1 → 強制対話（手動運用ケース）
            APP_NO_INTERACTIVE=1 または stdin が TTY でない → unattended
    """
    if os.environ.get("APP_KEEP_BROWSER", "").strip() in ("1", "true", "yes"):
        return False
    if os.environ.get("APP_NO_INTERACTIVE", "").strip() in ("1", "true", "yes"):
        return True
    try:
        return not sys.stdin.isatty()
    except Exception:
        return True


def _notify_discord(message: str) -> None:
    """app_notify.sh 経由で Discord に通知を送る。失敗は黙殺。"""
    import subprocess
    notify = Path(__file__).parent / "app_notify.sh"
    if not notify.exists():
        return
    try:
        subprocess.run(
            ["bash", str(notify), message],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _load_dashboard_config_for_brand() -> dict:
    """ブランド表示用の dashboard_config を最小限ロード（オーバーレイのタイトル用）"""
    try:
        p = CONFIG_DIR / "dashboard_config.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

DEFAULT_SETTINGS = {
    "provider": "gemini",          # "gemini" / "chatgpt" / "claude" / "codex"
    "model": "gemini-3-flash-preview",
    "api_key": "",                 # 後から設定（claude / codex は不要）
    "claude_cli": "claude",        # claude CLI のコマンド名（PATH上）
    "codex_cli": "codex",          # codex CLI のコマンド名（PATH上）
    "generation_mode": "styles_title_only",  # "lyrics", "lyrics_styles", "styles_title_only"
    "prompt": "Create a sophisticated lounge BGM track. Think elegant cafe, golden hour, luxury hotel lobby vibes. Instrumental only.",
    "loop_count": 5,
    "loop_interval_sec": 180,
    "headless": False,             # True にすると画面非表示
    "workspace": "",               # 指定時 SUNO ライブラリに同名ワークスペースを確保（例 "orzz_vol74"）
    "batch_mode": False,           # True なら CLI provider で N 曲分を事前生成してから順に投入
    # ── 歌詞・楽曲の多様性制御（チャンネル別に上書き可能。UI/設定から変更）──
    "diversity_threshold": 0.6,    # 類似度がこの値以上なら「似すぎ」とみなし作り直す（0.0〜1.0。1.0で実質無効）
    "diversity_retry": 2,          # 似すぎたときの最大再生成回数（0で再生成しない）
    "history_limit": 40,           # チャンネル別に保持する曲履歴件数（0で履歴・類似回避を無効化）
}

APPEND_PROMPT_STYLES_TITLE_ONLY = """

【Output Format】
title:
[English title here]

styles:
[Comma-separated Suno styles IN ENGLISH ONLY. e.g.: smooth jazz, lo-fi, ambient, cinematic, chill]

【Rules】
- ALL output MUST be in English. No Japanese.
- Only output title and styles, nothing else.
- Styles must be comma-separated English genre/mood tags suitable for Suno AI.
- STRICT: no real artist/band/composer/label names, no real song/album titles. Use generic descriptors only. Examples to AVOID: "Nujabes style", "like Bill Evans", "Lofi Girl vibe"."""

APPEND_PROMPT_WITH_STYLES = """

【出力形式】
title:
[ここにタイトル]

styles:
[ここにSuno用のStylesを英語のカンマ区切りで記載（プロンプトに合ったもの）]

lyrics:
[ここから下に歌詞]

【注意事項】
レスポンスには出力形式欄に記載したものだけを書いてください。
つまり、title、styles、lyrics以外の情報は記載しないでください。
STRICT: title/styles/lyrics に実在のアーティスト名・作曲家名・レーベル名・楽曲名を一切含めないでください（例: Nujabes, Bill Evans, Lofi Girl, Ghibli など）。SUNO が拒否します。ジャンル・ムード・楽器・情景の一般表現のみ使用してください。"""

APPEND_PROMPT_WITHOUT_STYLES = """

【出力形式】
title:
[ここにタイトル]

lyrics:
[ここから下に歌詞]

【注意事項】
レスポンスには出力形式欄に記載したものだけを書いてください。
つまり、titleとlyrics以外の情報は記載しないでください。
歌詞以外の余計な文章はlyrics欄に記載しないでください。
STRICT: title/lyrics に実在のアーティスト名・作曲家名・レーベル名・楽曲名を一切含めないでください。SUNO が拒否します。"""


# ─── SUNO 安全制約（実在アーティスト/曲名回避）────────────────
# SUNO は実在の固有名詞を含むプロンプトを拒否する。全テンプレ共通で末尾に付与。

SUNO_SAFETY_CLAUSE = """
- STRICT: do NOT include any real artist names, band names, composer/producer names, label names, channel names, or real song/album titles anywhere in `title`, `styles`, or `lyrics`. SUNO rejects such prompts. Use only generic descriptors (genre/mood/instrument/scene). Examples to AVOID: "Nujabes style", "like Bill Evans", "Lofi Girl vibe", "Ghibli soundtrack". Examples OK: "warm jazz trio", "cinematic strings", "dusty vinyl feel"."""


# ─── Suno META タグ命令（歌あり前提・Ghost Writer userscript と同一形式）──
# lyrics / lyrics_styles（=歌あり）モードの歌詞に、冒頭と各セクション先頭へ
# Suno の META タグ（[Mood:][Instrument:][Vocal Tone:][Vocal FX:][Energy:][Hook:]）と
# セクションタグ（[Intro][Verse][Pre-Chorus][Chorus][Bridge][Outro]）を付与させる。
APPEND_META_TAG_INSTRUCTION = """

【Suno METAタグについて】
歌詞の冒頭と各セクションの先頭に、以下のSuno METAタグを英語で付加してください。
曲の内容とプロンプトの世界観、styles の音楽性、各セクションの展開に一致するよう、曲の雰囲気・テンポに合わせて最適な値を選んで生成してください。

使えるMETAタグ（すべて英語）:
[Mood: <値>]  ← 例: Dreamy / Energetic / Nostalgic / Dark / Melancholic / Euphoric
[Instrument: <値>]  ← 例: Piano / Electric Guitar / Synth Pad / Strings / 808 Bass
[Vocal Tone: <値>]  ← 例: Whisper / Powerful / Soft / Raspy / Falsetto
[Vocal FX: <値>]  ← 例: Reverb+Echo / AutoTune / Delay / Chorus
[Energy: <値>]  ← 例: Low / Medium / High / Intense
[Hook: Yes]

セクションタグ（使用必須）: [Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro] など

形式例:
[Mood: Nostalgic] [Instrument: Piano] [Energy: Low]
[Intro]
...イントロの歌詞...

[Mood: Euphoric] [Energy: High] [Vocal FX: Reverb+Echo]
[Chorus]
...サビの歌詞...

※ METAタグは歌詞テキストの一部として出力してください。説明文は不要です。"""


# ─── Claude CLI 用: JSON 単一オブジェクト出力プロンプト ─────────────
# Claude CLI は対話的レスポンスを含みがちなので、JSON のみを厳格指示する

APPEND_PROMPT_JSON_STYLES_TITLE_ONLY = """

【Output Format — JSON ONLY】
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{
  "title": "<English title>",
  "styles": "<comma-separated English Suno styles INCLUDING song structure>"
}
Rules:
- All values MUST be English.
- `styles` must describe BOTH musical characteristics AND song structure.
  Include: genre/mood tags (e.g. `smooth jazz, lo-fi`), BPM (e.g. `120 BPM`),
  instruments (e.g. `piano solo intro, smooth saxophone`),
  and structural hints (e.g. `5-min track, gradual buildup, 0-5s piano only, 5-30s percussion layers in, 30s+ full arrangement, outro fade out`).
- Output ONLY the JSON object. No prose before or after.""" + SUNO_SAFETY_CLAUSE

APPEND_PROMPT_JSON_LYRICS_STYLES = """

【Output Format — JSON ONLY】
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{
  "title": "<song title>",
  "styles": "<comma-separated English Suno styles>",
  "lyrics": "<full sung lyrics WITH Suno META tags and section tags>"
}
Rules:
- THIS IS A VOCAL SONG. `lyrics` MUST contain real, singable lyrics (verses, choruses, etc.) in the language implied by the prompt (Japanese prompt → Japanese lyrics; English prompt → English lyrics).
- `styles` must describe genre, BPM, mood, and instruments (English, comma-separated). e.g. `dreamy city pop, 95 BPM, warm electric piano, gated reverb drums, female vocal`.
- `lyrics` MUST embed Suno META tags and section tags as described in the META section below.
- Output ONLY the JSON object. No prose before or after.""" + APPEND_META_TAG_INSTRUCTION + SUNO_SAFETY_CLAUSE

APPEND_PROMPT_JSON_LYRICS = """

【Output Format — JSON ONLY】
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{
  "title": "<song title>",
  "lyrics": "<full sung lyrics WITH Suno META tags and section tags>"
}
Rules:
- THIS IS A VOCAL SONG. `lyrics` MUST contain real, singable lyrics in the language implied by the prompt (Japanese prompt → Japanese lyrics; English prompt → English lyrics).
- `lyrics` MUST embed Suno META tags and section tags as described in the META section below.
- Output ONLY the JSON object. No prose before or after.""" + APPEND_META_TAG_INSTRUCTION + SUNO_SAFETY_CLAUSE


# ─── Claude CLI 一括生成用プロンプト（N 曲分を 1 回で返させる）───

APPEND_PROMPT_JSON_BATCH = """

【Output Format — JSON ONLY, BATCH MODE】
Generate {count} distinct songs in one response. Each song should have different mood/energy.
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{{
  "songs": [
    {{
      "title": "<English title>",
      "styles": "<genre, BPM, instruments, structural hints>",
      "lyrics": "<optional - only if lyrics mode>"
    }},
    ...
  ]
}}
Rules:
- Each `styles` MUST include: genre tags, BPM, instruments, and structural hints
  (e.g. `0-5s piano intro, 30s+ full mix, outro fade`).
- For `{mode_hint}` mode: {mode_rule}
- Titles should be distinct and evocative.
- Output ONLY the JSON object. No prose before or after.""" + SUNO_SAFETY_CLAUSE


# ─── LLM API 呼び出し ─────────────────────────────────

def call_gemini(api_key, model, prompt):
    """Gemini API を呼び出してテキスト生成"""
    import urllib.request
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    data = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 4096}
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise Exception(f"Gemini API エラー: {result['error'].get('message', '')}")
    candidates = result.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    raise Exception("Gemini から有効なレスポンスがありません")


def call_chatgpt(api_key, model, prompt):
    """ChatGPT API を呼び出してテキスト生成"""
    import urllib.request
    url = "https://api.openai.com/v1/chat/completions"
    data = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that writes song lyrics and styles."},
            {"role": "user", "content": prompt}
        ]
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    })
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise Exception(f"ChatGPT API エラー: {result['error'].get('message', '')}")
    choices = result.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    raise Exception("ChatGPT から有効なレスポンスがありません")


def call_claude_cli(cli_cmd, prompt, timeout=180):
    """claude CLI でテキスト生成（API不使用）。Claude→Codex フォールバック共通ランナー経由。

    provider="claude" 選択時、Claude が上限/エラーで失敗したら自動で Codex CLI に引き継ぐ
    （全機能のバックアップ回路）。provider="codex" 明示時は call_codex_cli を使う。
    """
    from app_llm_runner import run_llm
    return run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout, label="suno-ghostwriter")


def call_codex_cli(cli_cmd, prompt, timeout=300):
    """codex CLI を非対話で呼び出しテキスト生成（APIキー不使用）。

    `codex exec` は進捗ログを stdout に混ぜる場合があるため、
    --output-last-message のファイルを優先して読む。
    """
    import os
    import shutil
    import subprocess
    import tempfile

    cli_path = shutil.which(cli_cmd) or cli_cmd
    fd, out_path = tempfile.mkstemp(prefix="orzz_codex_", suffix=".txt")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                cli_path,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-last-message",
                out_path,
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            raise Exception(f"codex CLI エラー (rc={proc.returncode}): {err}")
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            text = ""
        return text or proc.stdout or ""
    except FileNotFoundError:
        raise Exception(
            f"codex CLI が見つかりません: '{cli_cmd}'. "
            f"Codex CLI をインストールしてください。"
        )
    except subprocess.TimeoutExpired:
        raise Exception(f"codex CLI タイムアウト ({timeout}s)")
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass


def _extract_json_object(text):
    """文字列からJSONオブジェクトを抽出（コードフェンス/前後文を無視）"""
    if not text:
        return None

    # ```json ... ``` フェンスを剥がす
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text

    # 最初の '{' から最後の '}' までを抽出（素直な貪欲マッチ）
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None

    blob = candidate[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # 一般的な壊れパターン: 末尾カンマ
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# ─── Instrumental filler ─────────────────────────────────────────────
# SUNO の Lyrics 欄を [instrumental] の繰り返しで埋める専用モード。
# AI に歌詞生成を依頼せず、機械的に固定文字列で 5000 文字を充填する。
INSTRUMENTAL_TOKEN = "[instrumental]"
INSTRUMENTAL_TARGET_CHARS = 5000

def build_instrumental_filler(target_chars: int = INSTRUMENTAL_TARGET_CHARS) -> str:
    """[instrumental] を改行区切りで繰り返し、target_chars 文字以下に収める。

    末尾切り詰めはせず、トークン単位で完結させる（途切れた `[instr` を残さない）。
    """
    token = INSTRUMENTAL_TOKEN + "\n"
    if target_chars < len(INSTRUMENTAL_TOKEN):
        return INSTRUMENTAL_TOKEN
    # 何個入るか（最後はトークン本体だけで改行不要）
    # 行数 N に対して総文字数 = (len(token) * (N-1)) + len(INSTRUMENTAL_TOKEN)
    n = max(1, (target_chars - len(INSTRUMENTAL_TOKEN)) // len(token) + 1)
    body = token * (n - 1) + INSTRUMENTAL_TOKEN
    return body


# ─── 歌詞・タイトルの多様性制御（チャンネル別履歴・類似曲回避）──────────
# Ghost Writer userscript (suno-ai-lyrics-creator.user.js) の多様性ロジックを移植。
# 履歴は channels.json の channel id 単位で .suno_history.json に永続化する。

SIMILARITY_THRESHOLD = 0.6      # これ以上似ていたら「類似」とみなす
MAX_DIVERSITY_RETRY = 2         # 似ていたとき作り直す最大回数
RECENT_SONG_LIMIT = 40          # 曲履歴の保持件数（チャンネル別）
RECENT_TITLE_LIMIT = 20         # タイトル履歴の保持件数（チャンネル別）

def _registry_dir():
    """channels.json / .suno_history.json を置く設定ディレクトリを解決。

    共有レジストリ（<repo>/config、app.py が同期する正本）を優先し、
    無ければ従来の CONFIG_DIR（~/.config/orzz）にフォールバックする。
    履歴を共有側に置くことで、複数PC運用でも多様性（重複回避）が持続する。
    """
    try:
        shared = Path(__file__).resolve().parent.parent / "config"
        if (shared / "channels.json").exists():
            return shared
    except Exception:
        pass
    return CONFIG_DIR


def _suno_history_path():
    return _registry_dir() / ".suno_history.json"

LYRIC_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "of", "for", "with",
    "is", "are", "was", "were", "be", "been", "im", "it", "its", "i", "you", "we", "he",
    "she", "they", "my", "your", "me", "us", "this", "that", "these", "those", "so",
    "just", "all", "no", "not", "do", "dont", "can", "will", "now", "up", "down",
    "like", "oh", "yeah", "na", "la", "ooh", "uh", "hey", "let", "lets", "got", "get",
}
OVERUSED_TITLE_WORDS = [
    "夜", "夢", "星", "光", "影", "君", "愛", "涙", "未来", "心",
    "moon", "night", "dream", "star", "light", "shadow", "love", "tears", "heart",
]


def _clamp_float(value, default, lo, hi):
    try:
        if value in (None, ""):
            return default
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _clamp_int(value, default, lo, hi):
    try:
        if value in (None, ""):
            return default
        return max(lo, min(hi, int(float(value))))
    except (TypeError, ValueError):
        return default


def _diversity_params(settings):
    """settings から多様性パラメータの実効値を取得（チャンネル別に上書き可能）。

    戻り値: (threshold: float, retry: int, song_limit: int)
      threshold … 類似度しきい値（既定 SIMILARITY_THRESHOLD、範囲 0.0〜1.0）
      retry     … 再生成上限（既定 MAX_DIVERSITY_RETRY、範囲 0〜5）
      song_limit… 曲履歴の保持件数（既定 RECENT_SONG_LIMIT、範囲 0〜200。0で履歴・類似回避を無効化）
    """
    threshold = _clamp_float(settings.get("diversity_threshold"), SIMILARITY_THRESHOLD, 0.0, 1.0)
    retry = _clamp_int(settings.get("diversity_retry"), MAX_DIVERSITY_RETRY, 0, 5)
    song_limit = _clamp_int(settings.get("history_limit"), RECENT_SONG_LIMIT, 0, 200)
    return threshold, retry, song_limit


def _sim_tokenize_words(text):
    t = (text or "").lower()
    t = re.sub(r"\[[^\]]*\]", " ", t)                 # [Mood] 等の META/セクションタグを除去
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)  # 記号を除去
    return [w for w in t.split() if len(w) >= 2]


def _sim_styles_tokens(styles):
    return {s.strip() for s in re.split(r"[,、\n]", (styles or "").lower()) if s.strip()}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _lyric_word_set(song):
    words = song.get("words")
    if words:
        return set(words)
    return set(_sim_tokenize_words(song.get("lyrics", "")))


def _song_similarity(a, b):
    """2曲の類似度（0〜1）。styles と lyrics(語) を主軸に、title を補助に。"""
    s_styles = _jaccard(_sim_styles_tokens(a.get("styles", "")), _sim_styles_tokens(b.get("styles", "")))
    wa, wb = _lyric_word_set(a), _lyric_word_set(b)
    s_lyrics = _jaccard(wa, wb)
    s_title = _jaccard(set(_sim_tokenize_words(a.get("title", ""))), set(_sim_tokenize_words(b.get("title", ""))))
    has_lyrics = bool(wa) and bool(wb)
    # 歌詞が無い（styles_title_only/instrumental）場合は styles 重視に寄せる
    if has_lyrics:
        return 0.45 * s_styles + 0.45 * s_lyrics + 0.10 * s_title
    return 0.80 * s_styles + 0.20 * s_title


def _max_similarity_against(song, others):
    return max((_song_similarity(song, o) for o in others), default=0.0)


def _extract_keywords(lyrics, limit=15):
    freq = {}
    for w in _sim_tokenize_words(lyrics):
        if w in LYRIC_STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


def _get_overused_words(recent_songs, min_count=2, limit=25):
    freq = {}
    for s in recent_songs:
        for w in (s.get("words") or []):
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
    return [w for w, c in ranked if c >= min_count][:limit]


def _clean_title_for_history(title):
    t = re.sub(r'^["\'「『\[]+|["\'」』\]]+$', "", str(title or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:80]


def _build_avoid_instruction(avoid_songs, strong=False, overused_words=None):
    parts = []
    if avoid_songs:
        lines = "\n".join(
            f"{i + 1}. title: {s.get('title') or '-'} / styles: {s.get('styles') or '-'}"
            for i, s in enumerate(avoid_songs[-8:])
        )
        head = (
            "\n\n【最重要・多様性の強制】直前の生成が下記の既存曲と似すぎました。ムード・ジャンル・楽器編成・テンポ・歌詞テーマ・コード進行感を大きく変え、明確に異なる曲にしてください。"
            if strong else
            "\n\n【多様性の確保】このチャンネル（過去に作成した曲を含む）で既に作った下記の曲と、ムード・ジャンル・楽器・テンポ・歌詞テーマが被らないようにし、聴き手が連続再生で飽きないよう変化をつけてください。"
        )
        parts.append(f"{head}\n{lines}")
    if overused_words:
        parts.append(
            "\n\n【単語・主題の重複回避】最近の曲で繰り返し登場している下記の語・モチーフは、今回の歌詞・タイトル・stylesでは使わないでください。毎回同じ題材（例: コーヒーの歌ばかり）にならないよう、別の題材・情景・語彙を選んでください:\n"
            + ", ".join(overused_words)
        )
    return "".join(parts)


def _build_title_instruction(recent_titles):
    rt = [t for t in (recent_titles or []) if t][:8]
    recent_block = ("\n\n【直近タイトル（似せない）】\n" + "\n".join(f"- {t}" for t in rt)) if rt else ""
    seed = "%06x" % random.randrange(16 ** 6)
    return (
        "\n\n【タイトル生成ルール】\n"
        f"今回のタイトル発想ID: {seed}\n"
        "・プロンプトや歌詞の中心モチーフから、具体的な名詞・場所・動作・手触りを1つ選び、短く印象的なタイトルにしてください。\n"
        "・Styles欄に出すジャンル、ムード、楽器、テンポ感を先に整理し、その楽曲の雰囲気に合うタイトルにしてください。タイトルとstylesの温度感・時代感・質感が矛盾しないようにしてください。\n"
        f"・毎回似た言葉に寄らないよう、定番語（{' / '.join(OVERUSED_TITLE_WORDS)}）は主題に必須な場合だけ使ってください。\n"
        "・「〜の歌」「〜の夜」「〜へ」「〜を抱いて」のような汎用テンプレートを避け、曲ごとの固有の情景を出してください。\n"
        "・タイトルは1案だけ。引用符、括弧、説明文、候補リストは不要です。\n"
        "・日本語曲なら日本語タイトル、英語曲なら英語タイトルを基本にしてください。" + recent_block
    )


def _is_vocal_mode(mode):
    """歌あり（実歌詞＋META タグ）モードか。instrumental 系は False。"""
    return mode in ("lyrics", "lyrics_styles")


# ── チャンネル別履歴の永続化（.suno_history.json）──

def _channel_id_from_settings(settings):
    """履歴キーに使う channel id を決定。

    優先度: settings["channel_id"] 明示 > workspace/video_name を channels.json と照合 > "default"。
    照合は各エントリの id / prefix / sanitize(name) のいずれかに対し、
    完全一致 または `<candidate>_` 前方一致（workspace は通常 `{name}_vol{N}` 形式）で判定する。
    """
    cid = (settings.get("channel_id") or "").strip()
    if cid:
        return cid
    hint = (settings.get("workspace") or settings.get("video_name") or "").strip().lower()
    if not hint:
        return "default"

    def _san(s):
        return re.sub(r"[^a-z0-9_-]+", "_", (s or "").lower()).strip("_")

    try:
        chans = json.loads((_registry_dir() / "channels.json").read_text(encoding="utf-8"))
    except Exception:
        chans = []
    for ch in chans:
        ch_id = (ch.get("id") or "").strip()
        candidates = {c for c in ((ch.get("prefix") or "").lower(), _san(ch.get("name")), ch_id.lower()) if c}
        for c in candidates:
            if hint == c or hint.startswith(c + "_"):
                return ch_id or "default"
    return "default"


def _load_suno_history():
    try:
        return json.loads(_suno_history_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_suno_history(state):
    path = _suno_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ .suno_history.json 保存失敗: {e}")


def _channel_history(state, channel_id):
    """(songs, titles) を返す。"""
    entry = state.get(channel_id)
    if not isinstance(entry, dict):
        return [], []
    return (entry.get("songs") or [], entry.get("titles") or [])


def _record_recent_song(state, channel_id, song, song_limit=RECENT_SONG_LIMIT):
    if song_limit <= 0:
        return  # 履歴無効化（history_limit=0）
    entry = state.get(channel_id)
    if not isinstance(entry, dict):
        entry = {}
    songs = entry.get("songs") or []
    titles = entry.get("titles") or []
    songs.append({
        "title": song.get("title", ""),
        "styles": song.get("styles", ""),
        "words": _extract_keywords(song.get("lyrics", "")),
    })
    entry["songs"] = songs[-song_limit:]
    clean = _clean_title_for_history(song.get("title", ""))
    if clean:
        merged = [clean] + [t for t in titles if _clean_title_for_history(t) != clean]
        entry["titles"] = merged[:min(RECENT_TITLE_LIMIT, song_limit)]
    state[channel_id] = entry


def generate_content_batch(settings, count):
    """CLI provider で N 曲分のメタデータを一度に生成してリストで返す。

    LLM の往復を count→1 に圧縮。結果は
    [{"title": ..., "styles": ..., "lyrics": ..., "mode": ...}, ...] のリスト。

    instrumental_filler モード時は、AI に歌詞生成を依頼せず
    タイトル + styles だけ取得し、lyrics は build_instrumental_filler() で固定生成する。
    """
    mode = settings["generation_mode"]
    base_prompt = settings["prompt"]
    provider = settings["provider"]

    if provider not in ("claude", "codex"):
        raise RuntimeError("batch モードは Claude/Codex CLI のみ対応です")

    # instrumental_filler は内部的には styles_title_only として AI を呼び、
    # lyrics は機械生成で上書きする
    effective_mode = "styles_title_only" if mode == "instrumental_filler" else mode
    if effective_mode == "styles_title_only":
        mode_hint, mode_rule = "styles_title_only", "omit the `lyrics` field from each song object."
    elif effective_mode == "lyrics_styles":
        mode_hint, mode_rule = "lyrics_styles", "include `lyrics` with FULL SUNG LYRICS plus Suno META tags ([Mood:][Instrument:][Energy:] ...) and section tags ([Intro][Verse][Chorus][Bridge][Outro])."
    else:
        mode_hint, mode_rule = "lyrics", "include `lyrics` with FULL SUNG LYRICS plus Suno META tags and section tags."

    # 履歴に基づく多様性指示（avoid + title）と、歌ありモードの META 指示を付加
    threshold, retry, song_limit = _diversity_params(settings)
    state = _load_suno_history() if song_limit > 0 else {}
    channel_id = _channel_id_from_settings(settings)
    hist_songs, hist_titles = _channel_history(state, channel_id) if song_limit > 0 else ([], [])
    overused = _get_overused_words(hist_songs)
    diversity_suffix = (
        _build_avoid_instruction(hist_songs, strong=False, overused_words=overused)
        + _build_title_instruction(hist_titles)
    )
    meta_suffix = APPEND_META_TAG_INSTRUCTION if _is_vocal_mode(mode) else ""

    full_prompt = base_prompt + diversity_suffix + meta_suffix + APPEND_PROMPT_JSON_BATCH.format(
        count=count, mode_hint=mode_hint, mode_rule=mode_rule,
    )
    if provider == "codex":
        cli_cmd = settings.get("codex_cli", "codex")
        print(f"  Codex CLI 一括呼び出し中... ({count}曲分)")
        response = call_codex_cli(cli_cmd, full_prompt, timeout=600)
    else:
        cli_cmd = settings.get("claude_cli", "claude")
        print(f"  Claude CLI 一括呼び出し中... ({count}曲分)")
        response = call_claude_cli(cli_cmd, full_prompt, timeout=300)
    obj = _extract_json_object(response)
    if not obj or "songs" not in obj or not isinstance(obj["songs"], list):
        raise Exception(f"{provider} CLI の出力からsongs配列を抽出できませんでした: {response[:200]}")

    instrumental_filler = (mode == "instrumental_filler")
    instrumental_lyrics = build_instrumental_filler() if instrumental_filler else ""

    def _norm(s):
        return {
            "title": str(s.get("title", "")).strip(),
            "styles": str(s.get("styles", "")).strip(),
            "lyrics": instrumental_lyrics if instrumental_filler else str(s.get("lyrics", "")).strip(),
            "mode": mode,
        }

    raw_songs = [_norm(s) for s in obj["songs"][:count]]

    # ── 多様性チェック: 履歴＋同バッチ確定済みに似すぎる曲は個別に再生成して差し替え ──
    accepted = []
    for idx, song in enumerate(raw_songs, 1):
        avoid_pool = hist_songs + accepted
        sim = _max_similarity_against(song, avoid_pool)
        best = (song, sim)
        if avoid_pool and sim >= threshold and retry > 0:
            for attempt in range(1, retry + 1):
                print(f"  ↻ バッチ {idx}/{count}: 類似度 {sim:.2f} ≥ {threshold} → 個別再生成 ({attempt}/{retry})")
                try:
                    regen = _generate_content_once(
                        settings, avoid_songs=avoid_pool, strong=True,
                        recent_titles=hist_titles, overused=overused,
                    )
                    if instrumental_filler:
                        regen["lyrics"] = instrumental_lyrics
                    rsim = _max_similarity_against(regen, avoid_pool)
                    if rsim < best[1]:
                        best = (regen, rsim)
                    if rsim < threshold:
                        break
                    sim = rsim
                except Exception as e:
                    print(f"  ⚠️ 個別再生成失敗: {e}")
                    break
        accepted.append(best[0])
        _record_recent_song(state, channel_id, best[0], song_limit=song_limit)

    if song_limit > 0:
        _save_suno_history(state)

    if instrumental_filler:
        print(f"  ✅ {len(accepted)}曲分のメタデータを取得（lyrics は [instrumental] x {INSTRUMENTAL_TARGET_CHARS}文字 で充填）")
    else:
        print(f"  ✅ {len(accepted)}曲分のメタデータを取得しました（多様性チェック済み）")
    return accepted


# ─── 混成（複数 Style）ドラフト生成 ──────────────────────────────────────────
# SUNO は 1 prompt = 1 Style なので、ボサ/R&B × 女/男 のような「混成 N 曲」を作るには
# Style グループごとに generate_content_batch を呼んで統合する必要がある。
# 従来は /tmp/sukima_mix20_draft_gen.py にハードコードしていたが、S6 で
# `.app_channel_config.json` の `suno.cozy_mix` ブロックを読む config 駆動に正規化した。

# cozy_mix.mix の "<kind>_<vocal>" キー → (style キー, vocal, draft genre タグ) の対応。
# draft genre は整備 step(_tag_args_from_channel/genre_by_kind)で ID3 ジャンルに使うため
# config.tag_defaults.genre_by_kind のキー（bossa / rnb）に揃える。
_MIX_GROUP_MAP = {
    "bossa_female": ("bossa_female_style", "female", "bossa"),
    "bossa_male":   ("bossa_male_style",   "male",   "bossa"),
    "rnb_female":   ("rnb_female_style",   "female", "rnb"),
    "rnb_male":     ("rnb_male_style",     "male",   "rnb"),
}


def generate_mixed_drafts(channel_config, provider=None, claude_cli=None,
                          codex_cli=None, channel_id=None, workspace=None,
                          diversity_threshold=None, diversity_retry=None,
                          history_limit=None):
    """`suno.cozy_mix` を読み、Style グループごとに混成ドラフトを生成して統合する。

    /tmp/sukima_mix20_draft_gen.py（vol.2 で確立）を config 駆動に正規化したもの。
    各 Style に lyric_guide（cozy 注入・Intro 多様化・da-da-da 禁止・脱 lo-fi）を付け、
    mix の比率（bossa_female:7 等）の数だけ generate_content_batch を呼ぶ。

    Args:
        channel_config: per-channel `.app_channel_config.json` を読んだ dict。
                        `suno.cozy_mix.{*_style, lyric_guide, mix}` を参照する。
        provider:       LLM provider 上書き。未指定なら DRAFT_PROVIDER env →
                        suno.provider → "codex"（300s timeout 回避のため codex 推奨）。
        その他:          generate_content_batch に渡す settings の上書き（履歴/多様性）。

    Returns:
        統合済み songs リスト。各要素は {title, styles, lyrics, mode, vocal, genre}。
        vocal/genre は後段（整備/タグ付与）が曲別 ID3 ジャンルを決めるために付与する。

    Raises:
        ValueError: cozy_mix ブロックが無い / mix が空 / style キー欠落の場合。
    """
    suno_cfg = (channel_config or {}).get("suno") or {}
    cozy = suno_cfg.get("cozy_mix") or {}
    if not cozy:
        raise ValueError(
            "channel_config.suno.cozy_mix がありません。"
            "混成ドラフト生成には cozy_mix（*_style / lyric_guide / mix）が必要です。"
        )
    mix = cozy.get("mix") or {}
    # `_total` / `_summary` 等のメタキー（_ 始まり）を除外して有効な比率だけ拾う
    mix_items = {k: v for k, v in mix.items()
                 if not str(k).startswith("_") and isinstance(v, int) and v > 0}
    if not mix_items:
        raise ValueError("channel_config.suno.cozy_mix.mix に有効な曲数指定がありません。")

    lyric_guide = cozy.get("lyric_guide") or ""

    # provider 解決: 明示 > DRAFT_PROVIDER env > suno.provider > codex
    eff_provider = (
        (provider or "").strip()
        or (os.environ.get("DRAFT_PROVIDER") or "").strip()
        or (suno_cfg.get("provider") or "").strip()
        or "codex"
    )
    if eff_provider not in ("claude", "codex"):
        # Style 混成は CLI 一括生成（generate_content_batch）前提。
        # gemini/chatgpt は未対応なので codex にフォールバック。
        print(f"  ⚠ provider={eff_provider} は混成ドラフト非対応 → codex にフォールバック")
        eff_provider = "codex"

    base = {
        "provider": eff_provider,
        "model": suno_cfg.get("model") or "cli-default",
        "claude_cli": claude_cli or suno_cfg.get("claude_cli") or "claude",
        "codex_cli": codex_cli or suno_cfg.get("codex_cli") or "codex",
        "generation_mode": suno_cfg.get("generation_mode") or "lyrics_styles",
        "channel_id": channel_id or "sukima_cozy",
    }
    if workspace:
        base["workspace"] = workspace
    if diversity_threshold is not None:
        base["diversity_threshold"] = diversity_threshold
    if diversity_retry is not None:
        base["diversity_retry"] = diversity_retry
    if history_limit is not None:
        base["history_limit"] = history_limit

    # mix のキー順を _MIX_GROUP_MAP の宣言順（bossa→rnb, female→male）に正規化して安定化
    ordered_keys = [k for k in _MIX_GROUP_MAP if k in mix_items]
    ordered_keys += [k for k in mix_items if k not in _MIX_GROUP_MAP]

    all_songs = []
    for group_key in ordered_keys:
        count = mix_items[group_key]
        if group_key not in _MIX_GROUP_MAP:
            print(f"  ⚠ 未知の mix グループ '{group_key}' をスキップ（_MIX_GROUP_MAP に未定義）")
            continue
        style_key, vocal, genre = _MIX_GROUP_MAP[group_key]
        style_text = cozy.get(style_key)
        if not style_text:
            raise ValueError(
                f"channel_config.suno.cozy_mix.{style_key} がありません"
                f"（mix.{group_key}={count} の Style 文が必要）。"
            )
        st = dict(base)
        st["prompt"] = style_text + lyric_guide
        print(f"[mix-draft] {genre}/{vocal} x{count}（provider={eff_provider}）", flush=True)
        songs = generate_content_batch(st, count)
        for x in songs:
            x["vocal"] = vocal
            x["genre"] = genre
        all_songs.extend(songs)

    total = sum(mix_items.get(k, 0) for k in ordered_keys if k in _MIX_GROUP_MAP)
    print(f"\n[mix-draft] OK {len(all_songs)} songs（指定 {total} 曲）", flush=True)
    return all_songs


def _generate_content_once(settings, avoid_songs=None, strong=False, recent_titles=None, overused=None):
    """1曲分を生成して {title, styles, lyrics, mode} を返す（履歴記録なし・SUNO投入なし）。

    avoid_songs / recent_titles / overused が与えられた場合は多様性指示をプロンプトに付加する。
    歌あり（lyrics / lyrics_styles）モードでは META タグ指示も付与する（JSON プロンプト側に内蔵）。
    """
    mode = settings["generation_mode"]
    base_prompt = settings["prompt"]
    provider = settings["provider"]

    # instrumental_filler は AI には styles_title_only として依頼し、lyrics は機械生成で上書き
    effective_mode = "styles_title_only" if mode == "instrumental_filler" else mode

    # 履歴に基づく多様性指示（avoid + title）。プロンプト本文と出力形式指示の間に挟む。
    diversity_suffix = (
        _build_avoid_instruction(avoid_songs, strong=strong, overused_words=overused)
        + _build_title_instruction(recent_titles)
    )

    # CLI provider は JSON 出力用プロンプトを使用（歌ありの JSON プロンプトには META 指示を内蔵済み）
    if provider in ("claude", "codex"):
        if effective_mode == "styles_title_only":
            append = APPEND_PROMPT_JSON_STYLES_TITLE_ONLY
        elif effective_mode == "lyrics_styles":
            append = APPEND_PROMPT_JSON_LYRICS_STYLES
        else:
            append = APPEND_PROMPT_JSON_LYRICS
    else:
        # 非 CLI（gemini/chatgpt）は従来の自由形式。歌ありモードのみ META 指示を追記。
        meta = APPEND_META_TAG_INSTRUCTION if _is_vocal_mode(mode) else ""
        if effective_mode == "styles_title_only":
            append = APPEND_PROMPT_STYLES_TITLE_ONLY
        elif effective_mode == "lyrics_styles":
            append = APPEND_PROMPT_WITH_STYLES + meta
        else:
            append = APPEND_PROMPT_WITHOUT_STYLES + meta

    full_prompt = base_prompt + diversity_suffix + append

    api_key = settings.get("api_key", "")
    model = settings.get("model", "")

    if provider == "claude":
        cli_cmd = settings.get("claude_cli", "claude")
        print(f"  Claude CLI 呼び出し中... ({cli_cmd}, JSON出力モード)")
        response = call_claude_cli(cli_cmd, full_prompt)
    elif provider == "codex":
        cli_cmd = settings.get("codex_cli", "codex")
        print(f"  Codex CLI 呼び出し中... ({cli_cmd}, JSON出力モード)")
        response = call_codex_cli(cli_cmd, full_prompt)
    elif provider == "gemini":
        print(f"  LLM呼び出し中... (gemini / {model})")
        response = call_gemini(api_key, model, full_prompt)
    else:
        print(f"  LLM呼び出し中... (chatgpt / {model})")
        response = call_chatgpt(api_key, model, full_prompt)

    # レスポンスをパース
    title = ""
    styles = ""
    lyrics = ""

    if provider in ("claude", "codex"):
        # JSON 単一オブジェクトを期待
        obj = _extract_json_object(response)
        if not obj:
            raise Exception(f"{provider} CLI の出力からJSONを抽出できませんでした: {response[:200]}")
        title = str(obj.get("title", "")).strip()
        styles = str(obj.get("styles", "")).strip()
        lyrics = str(obj.get("lyrics", "")).strip()
    else:
        title_match = re.search(r"title:\s*\n?([^\n]*)", response, re.IGNORECASE)
        styles_match = re.search(r"styles:\s*\n?([\s\S]*?)(?:\n\s*lyrics:|$)", response, re.IGNORECASE)
        lyrics_match = re.search(r"lyrics:\s*\n?([\s\S]*)", response, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()
        if styles_match:
            styles = styles_match.group(1).strip()
        if lyrics_match:
            lyrics = lyrics_match.group(1).strip()

    # instrumental_filler: AI 出力の lyrics は捨て、固定の [instrumental] 充填で上書き
    if mode == "instrumental_filler":
        lyrics = build_instrumental_filler()

    return {"title": title, "styles": styles, "lyrics": lyrics, "mode": mode}


def generate_content(settings):
    """1曲を生成。チャンネル履歴に基づく多様性リトライ＋履歴記録つき。"""
    threshold, retry, song_limit = _diversity_params(settings)
    state = _load_suno_history() if song_limit > 0 else {}
    channel_id = _channel_id_from_settings(settings)
    songs, titles = _channel_history(state, channel_id) if song_limit > 0 else ([], [])
    overused = _get_overused_words(songs)

    chosen = None
    best = None  # (song, sim) — 閾値を超えられなかった場合に最も似ていない候補を採用
    for attempt in range(retry + 1):
        result = _generate_content_once(
            settings, avoid_songs=songs, strong=(attempt > 0),
            recent_titles=titles, overused=overused,
        )
        sim = _max_similarity_against(result, songs)
        if best is None or sim < best[1]:
            best = (result, sim)
        if not songs or sim < threshold:
            chosen = result
            break
        if attempt < retry:
            print(f"  ↻ 類似度 {sim:.2f} ≥ {threshold} → 多様化のため再生成 ({attempt + 1}/{retry})")

    if chosen is None and best is not None:
        chosen = best[0]
        print(f"  ⚠️ 閾値内に収まらず、最も似ていない候補を採用（類似度 {best[1]:.2f}）")

    title, styles, lyrics = chosen["title"], chosen["styles"], chosen["lyrics"]
    if chosen["mode"] == "instrumental_filler":
        print(f"  タイトル: {title}")
        print(f"  スタイル: {styles[:80]}...")
        print(f"  歌詞: [instrumental] x {INSTRUMENTAL_TARGET_CHARS}文字 で充填（{len(lyrics)}文字）")
    else:
        print(f"  タイトル: {title}")
        print(f"  スタイル: {styles[:80]}...")
        if lyrics:
            print(f"  歌詞: {lyrics[:50]}...")

    _record_recent_song(state, channel_id, chosen, song_limit=song_limit)
    if song_limit > 0:
        _save_suno_history(state)
    return chosen


# ─── ブラウザ自動操作 ──────────────────────────────────

# React の内部状態を更新するための JS コード
SET_REACT_VALUE_JS = """
(element, value, isTextarea) => {
    // React の nativeInputValueSetter で確実に値を設定
    const proto = isTextarea
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value').set;

    // まず focus
    element.focus();

    // native setter で値を設定
    nativeSetter.call(element, value);

    // React の valueTracker をリセット
    const tracker = element._valueTracker;
    if (tracker) tracker.setValue('');

    // React が検知するイベントを発火
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));

    // blur して確定
    element.dispatchEvent(new Event('blur', { bubbles: true }));
}
"""


def ensure_workspace(page, workspace_name):
    """SUNO /me/workspaces 経由で Workspace を作成（参考コード準拠）。

    手順:
      1. https://suno.com/me/workspaces へ遷移
      2. role="button" name="New Workspace" をクリック
      3. placeholder="Untitled Workspace" の入力欄に名前を入力
      4. role="button" name="Create Workspace" をクリック
      5. /create?wid=* にリダイレクトされるのを待機

    既存同名ワークスペースがあれば選択のみ行う。
    """
    if not workspace_name:
        return False

    print(f"\n▶ Workspace '{workspace_name}' を確保中...")
    _set_status(page, f"Workspace '{workspace_name}' を確保中...", "info")

    # /me/workspaces に遷移
    try:
        page.goto("https://suno.com/me/workspaces", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
    except Exception as e:
        print(f"  ⚠️ /me/workspaces アクセス失敗: {e}")
        _set_status(page, f"/me/workspaces アクセス失敗: {e}", "err")
        return False

    # 既に同名のワークスペースがあればクリックして選択（重複作成を避ける）
    # DOM 上の workspace カードは <div role="button"> で text に "orzz_vol77 80 Songs · 1d ago" のような形。
    # 部分一致 (has_text=workspace_name) は prefix 衝突を起こす（例: "Harbor_Notes_vol1" を検索すると
    # "Harbor_Notes_vol13" にもマッチし、DOM 順次第で誤った workspace を選んでしまう）。
    # 解決: 「ワークスペース名で始まり、その直後がスペースか終端」の境界判定 regex で exact match に近づける。
    try:
        # re.escape で workspace_name 内のメタ文字をエスケープし、(?:\s|$) で名前の後ろを境界化。
        ws_pattern = re.compile(rf"^{re.escape(workspace_name)}(?:\s|$)", re.MULTILINE)
        existing = page.locator('div[role="button"]').filter(has_text=ws_pattern).first
        if existing.count() > 0 and existing.is_visible():
            existing.click(timeout=3000)
            time.sleep(2)
            print(f"  ✓ 既存 Workspace '{workspace_name}' を選択しました")
            _set_status(page, f"既存 Workspace '{workspace_name}' を選択", "ok")
            # /create?wid= にリダイレクトされるのを待機
            try:
                page.wait_for_url("**/create?wid=*", timeout=8000)
            except Exception:
                if "/create" not in page.url:
                    page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
            return True
    except Exception:
        pass

    # 「New Workspace」ボタンをクリック (DOM確認済: <BUTTON> text="New Workspace")
    print("  ↳ 既存 WS なし。新規作成します")
    _set_status(page, "新規 Workspace を作成中...", "info")
    try:
        btn = page.get_by_role("button", name="New Workspace").first
        btn.click(timeout=5000)
        print("  ✓ 「New Workspace」クリック")
        time.sleep(1.5)
    except Exception as e:
        print(f"  ⚠️ 「New Workspace」ボタンが見つかりません: {e}")
        try:
            # フォールバック: text 完全一致で探す
            page.locator('button:has-text("New Workspace")').first.click(timeout=3000)
            print("  ✓ 「New Workspace」フォールバック成功")
            time.sleep(1.5)
        except Exception:
            # DOM ダンプ
            try:
                btns = page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('button, [role="button"]').forEach(b => {
                        out.push((b.textContent || '').trim().slice(0, 50));
                    });
                    return out.slice(0, 20);
                }""")
                print(f"    ボタン一覧: {btns}")
            except Exception:
                pass
            return False

    # 入力欄に名前を入力（ダイアログが開く。placeholder="Untitled Workspace"）
    try:
        input_box = page.get_by_placeholder("Untitled Workspace").first
        input_box.wait_for(state="visible", timeout=5000)
        input_box.fill(workspace_name, timeout=3000)
        time.sleep(0.5)
        print(f"  ✓ 名前入力: '{workspace_name}'")
    except Exception as e:
        print(f"  ⚠️ 入力欄 (placeholder=Untitled Workspace) が見つかりません: {e}")
        # フォールバック: visible な input[type=text] を探す
        try:
            inp = page.locator('input[type="text"]:visible').first
            inp.fill(workspace_name, timeout=3000)
            time.sleep(0.5)
            print(f"  ✓ 名前入力 (フォールバック): '{workspace_name}'")
        except Exception:
            return False

    # 「Create Workspace」ボタンをクリック
    try:
        create_btn = page.get_by_role("button", name="Create Workspace").first
        create_btn.click(timeout=5000)
        print("  ✓ 「Create Workspace」クリック")
    except Exception as e:
        # フォールバック: テキストで探す
        try:
            page.locator('button:has-text("Create Workspace")').first.click(timeout=3000)
            print("  ✓ 「Create Workspace」フォールバック成功")
        except Exception:
            print(f"  ⚠️ 「Create Workspace」が見つかりません: {e}")
            return False

    # /create?wid=* へのリダイレクトを待機
    try:
        page.wait_for_url("**/create?wid=*", timeout=10000)
        print(f"  ✅ Workspace '{workspace_name}' を作成しました (URL: {page.url})")
        _set_status(page, f"Workspace '{workspace_name}' 作成完了 ✅", "ok")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"  ⚠️ リダイレクト未検知 (URL: {page.url}): {e}")
        time.sleep(2)
        if "/create" in page.url:
            print(f"  ℹ️ /create にいるので続行します")
            _set_status(page, f"Workspace 作成 → 続行", "ok")
            return True
        try:
            page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
        except Exception:
            pass
        return True


def download_workspace_tracks(page, workspace_name, target_dir):
    """指定 Workspace の全楽曲を MP3 ダウンロード → target_dir に保存。

    Tampermonkey「Suno 選択 & 一括DL」(全選択 + フォルダ保存) と同じ手順:
      1. /me/workspaces で対象 Workspace を開く (/create?wid=XXX に遷移)
      2. 楽曲リストをスクロールして全 song UUID を収集 (DOM lazy-load 対策)
      3. 各 UUID について /api/feed/?ids=UUID を叩き audio_url + title 取得
      4. audio_url を context.request で取得 (Cookie 共有) → MP3 を保存
    """
    from pathlib import Path as _P
    target = _P(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    print(f"\n▶ Workspace '{workspace_name}' の楽曲を {target} にダウンロード...")
    _set_status(page, f"Workspace '{workspace_name}' を開いています...", "info")

    # 1) /me/workspaces へ遷移 → 対象 Workspace を開く
    try:
        page.goto("https://suno.com/me/workspaces", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
    except Exception as e:
        print(f"  ⚠️ /me/workspaces アクセス失敗: {e}")
        return 0

    try:
        target_ws = page.get_by_text(workspace_name, exact=True).first
        target_ws.wait_for(state="visible", timeout=5000)
        target_ws.click(timeout=3000)
        time.sleep(3)
        print(f"  ✓ Workspace 開く: {page.url}")
    except Exception as e:
        print(f"  ⚠️ Workspace '{workspace_name}' が見つかりません: {e}")
        return 0

    # 2) インターセプタが既に install されているか確認（add_init_script 経由）
    #    されていない場合はこの時点で evaluate 注入する（既存タブで呼ばれた場合の救済）
    installed = page.evaluate("() => !!window.__sunoAudioInterceptorInstalled")
    if not installed:
        print("  ℹ️ インターセプタ未インストール。現ページに後注入します（既存トラフィックの一部は取り逃す可能性）")
        page.evaluate(_SUNO_AUDIO_URL_INTERCEPTOR)

    # 3) スクロールしつつ全 song UUID を収集
    #    これにより SUNO SPA が内部で /api/feed 等を叩き、__sunoAudioUrlCache が埋まる
    _set_status(page, "楽曲一覧をスクロール取得中...", "info")
    uuids = _collect_all_song_uuids(page)
    print(f"  📋 検出楽曲数: {len(uuids)}")
    _set_status(page, f"楽曲検出: {len(uuids)}曲 / audio_url 収集中...", "info")
    if not uuids:
        print("  ⚠️ clip-row が見つかりません。DOM 構造が変わっている可能性")
        return 0

    # 4) キャッシュが埋まるまで少し待機（非同期 JSON パース完了待ち）
    time.sleep(2)
    cache = page.evaluate("() => window.__sunoAudioUrlCache || {}")
    cache_keys = list(cache.keys()) if cache else []
    print(f"  🗂 audio_url キャッシュ件数: {len(cache_keys)}")

    # キャッシュが不足している場合は再スクロールで再試行
    if len(cache_keys) < len(uuids) * 0.5:
        print(f"  ↻ キャッシュ不足 ({len(cache_keys)}/{len(uuids)})。再スクロールで補充...")
        _collect_all_song_uuids(page, from_top=True)
        time.sleep(3)
        cache = page.evaluate("() => window.__sunoAudioUrlCache || {}")
        cache_keys = list(cache.keys()) if cache else []
        print(f"  🗂 再収集後のキャッシュ: {len(cache_keys)}")

    # 5) 各 UUID について audio_url を引いて MP3 をダウンロード
    success = 0
    failed = 0
    used_names = set()
    ctx_request = page.context.request  # Cookie 共有済の APIRequestContext

    for idx, uuid in enumerate(uuids, 1):
        try:
            # インターセプタキャッシュから取得
            meta = cache.get(uuid)
            audio_url = None
            title = None
            if meta:
                audio_url = meta.get("audioUrl")
                title = meta.get("title")

            # キャッシュに無い場合は個別に /song/UUID ページを軽く踏むことで誘発する
            # （重いので最大 5 回まで）
            if not audio_url and failed < 5:
                try:
                    # ページ内で SPA の状態を叩きたいが、安全のため直接 fetch を試す
                    meta2 = page.evaluate("""async (uuid) => {
                        try {
                            const r = await fetch(`/api/feed/?ids=${uuid}`, {credentials:'include'});
                            if (!r.ok) return null;
                            const data = await r.json();
                            const clips = Array.isArray(data) ? data
                                        : Array.isArray(data && data.clips) ? data.clips
                                        : Array.isArray(data && data.data) ? data.data
                                        : data ? [data] : [];
                            const c = clips[0];
                            if (!c) return null;
                            const u = c.audio_url || c.audio || c.file_url || c.mp3_url;
                            if (!u) return null;
                            return {audioUrl: u, title: c.title || c.name || uuid};
                        } catch(e) { return null; }
                    }""", uuid)
                    if meta2:
                        audio_url = meta2.get("audioUrl")
                        title = meta2.get("title")
                except Exception:
                    pass

            if not audio_url:
                print(f"  ⚠️ [{idx}/{len(uuids)}] {uuid[:8]}... audio_url 取得失敗 (cache miss)")
                failed += 1
                continue

            # ファイル名を決定
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title or uuid)
            base = safe_title or f"track_{idx}"
            fname = f"{base}.mp3"
            counter = 2
            while fname in used_names:
                fname = f"{base}_{counter}.mp3"
                counter += 1
            used_names.add(fname)

            # MP3 バイナリを取得
            try:
                audio_resp = ctx_request.get(audio_url, timeout=60000)
                if not audio_resp.ok:
                    print(f"  ⚠️ [{idx}/{len(uuids)}] {fname} HTTP {audio_resp.status}")
                    failed += 1
                    continue
                body = audio_resp.body()
                if not body or len(body) < 1000:
                    print(f"  ⚠️ [{idx}/{len(uuids)}] {fname} 空データ")
                    failed += 1
                    continue
                save_path = target / fname
                save_path.write_bytes(body)
                size_mb = len(body) / 1024 / 1024
                print(f"  ✓ [{idx}/{len(uuids)}] {fname} ({size_mb:.1f}MB)")
                _set_status(page, f"DL {idx}/{len(uuids)}: {fname} ({size_mb:.1f}MB)", "ok")
                success += 1
            except Exception as e:
                print(f"  ⚠️ [{idx}/{len(uuids)}] {fname} 取得エラー: {e}")
                failed += 1
        except Exception as e:
            print(f"  ⚠️ [{idx}/{len(uuids)}] 予期せぬエラー: {e}")
            failed += 1

    print(f"\n  📥 完了: 成功 {success} / 失敗 {failed} / 総数 {len(uuids)}")
    _set_status(page, f"📥 完了: 成功 {success} / 失敗 {failed}", "ok" if failed == 0 else "warn")
    return success


def _collect_all_song_uuids(page, from_top: bool = False):
    """ページをスクロールしながら全ての [data-testid='clip-row'] から song UUID を収集

    from_top=True の場合はスクロールコンテナを先頭に戻してから走査（再補充用）
    """
    if from_top:
        try:
            page.evaluate("""() => {
                const rows = document.querySelectorAll('[data-testid="clip-row"]');
                if (rows.length === 0) return;
                let el = rows[0].parentElement;
                while (el) {
                    const s = window.getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                        el.scrollTop = 0;
                        return;
                    }
                    el = el.parentElement;
                }
                const doc = document.scrollingElement || document.documentElement;
                doc.scrollTop = 0;
            }""")
            time.sleep(1)
        except Exception:
            pass

    uuids_seen = []
    uuids_set = set()

    def _scan():
        result = page.evaluate("""() => {
            const rows = document.querySelectorAll('[data-testid="clip-row"]');
            const out = [];
            rows.forEach(r => {
                const a = r.querySelector('a[href*="/song/"]');
                if (!a) return;
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/song\\/([a-f0-9-]{8,})/i);
                if (m) out.push(m[1]);
            });
            return out;
        }""")
        for u in result:
            if u not in uuids_set:
                uuids_set.add(u)
                uuids_seen.append(u)

    # 初期スキャン
    time.sleep(2)
    _scan()
    print(f"    初期スキャン: {len(uuids_seen)} 件")

    # スクロール可能コンテナを探して頭→尻まで読み込む
    stable = 0
    for i in range(40):  # 最大40回スクロール (1ループ~250件分)
        before = len(uuids_seen)
        # スクロール実行
        moved = page.evaluate("""() => {
            const rows = document.querySelectorAll('[data-testid="clip-row"]');
            if (rows.length === 0) return false;
            // 親を辿ってスクロール可能コンテナを探す
            let el = rows[0].parentElement;
            while (el) {
                const s = window.getComputedStyle(el);
                if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                    const maxTop = el.scrollHeight - el.clientHeight;
                    if (el.scrollTop >= maxTop - 5) return false;
                    el.scrollTop = Math.min(el.scrollTop + el.clientHeight * 0.9, maxTop);
                    return true;
                }
                el = el.parentElement;
            }
            // ドキュメント全体をスクロール
            const doc = document.scrollingElement || document.documentElement;
            const maxTop = doc.scrollHeight - doc.clientHeight;
            if (doc.scrollTop >= maxTop - 5) return false;
            doc.scrollTop = Math.min(doc.scrollTop + doc.clientHeight * 0.9, maxTop);
            return true;
        }""")
        time.sleep(0.4)
        _scan()
        after = len(uuids_seen)
        if not moved:
            stable += 1
            if stable >= 3:
                break
        elif after == before:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
    return uuids_seen


def run_browser_automation(settings):
    """Playwright でブラウザを操作"""
    from playwright.sync_api import sync_playwright

    loop_count = settings.get("loop_count", 1)
    loop_interval = settings.get("loop_interval_sec", 180)
    headless = settings.get("headless", False)

    profile_dir = str(Path.home() / ".config/orzz/chromium_profile")

    with sync_playwright() as p:
        print("\nブラウザを起動中...")
        print(f"  プロファイル: {profile_dir}")

        # ブラウザ起動: Playwright管理のChromium → システムChrome の順でフォールバック
        launch_kwargs = dict(
            user_data_dir=profile_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"],
        )

        context = None

        # 方法1: Playwright 管理の Chromium（ポータブル・推奨）
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
            print("  Playwright Chromium で起動しました")
        except Exception as e1:
            print(f"  Playwright Chromium 失敗: {e1}")

            # 方法2: システムの Chrome を使用（フォールバック）
            try:
                context = p.chromium.launch_persistent_context(
                    channel="chrome", **launch_kwargs
                )
                print("  システム Chrome で起動しました")
            except Exception as e2:
                print(f"  システム Chrome も失敗: {e2}")
                print("")
                print("=" * 50)
                print("  ブラウザが見つかりません！")
                print("  以下を実行してください:")
                print("    python3 -m playwright install chromium")
                print("=" * 50)
                return

        # SUNO SPA の内部 fetch/XHR を横取りして audio_url をキャッシュ + ステータスオーバーレイ
        context.add_init_script(_SUNO_AUDIO_URL_INTERCEPTOR)
        # ブランド表示名を window に注入（オーバーレイのタイトルで使用）
        try:
            _dc = _load_dashboard_config_for_brand()
            _brand = (_dc.get("brand_short") or "Automation Studio").replace("'", "\\'")
        except Exception:
            _brand = "Automation Studio"
        context.add_init_script(f"window.__appBrandLabel = '{_brand}';")
        context.add_init_script(_STATUS_OVERLAY_SCRIPT)

        page = context.pages[0] if context.pages else context.new_page()

        # SUNO にアクセス
        print("suno.com/create にアクセス中...")
        page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=30000)
        _set_status(page, "SUNO /create にアクセス中...", "info")
        time.sleep(5)

        current_url = page.url
        print(f"現在のURL: {current_url}")

        # ログイン判定: suno.com にいて、かつ Create ボタンが存在するか確認
        def is_logged_in():
            try:
                # suno.com 上にいるか
                if "suno.com" not in page.url:
                    return False
                # Create ボタンまたは曲作成UIが存在するか
                buttons = page.query_selector_all('button')
                for btn in buttons:
                    try:
                        text = btn.inner_text().strip().lower()
                        if text in ('create', '作成'):
                            return True
                    except Exception:
                        continue
                # textarea (歌詞入力欄) が存在するか
                if page.query_selector('textarea'):
                    return True
                return False
            except Exception:
                return False

        if not is_logged_in():
            print("\n⚠️  SUNOにログインが必要です。")
            print("ブラウザでログインしてください。")
            print("ログイン後、suno.com/create ページに自動で移動します。")
            print("待機中... (最大5分)")

            # ログイン完了を自動検知（最大5分待機）
            for tick in range(300):
                time.sleep(1)
                if tick % 10 == 0 and tick > 0:
                    print(f"  ... 待機中 ({tick}秒経過) URL: {page.url}")
                try:
                    # suno.com のどこかにいればリダイレクト試行
                    if "suno.com" in page.url and "sign" not in page.url.lower() and "login" not in page.url.lower() and "clerk" not in page.url.lower():
                        # /create に移動
                        if "/create" not in page.url:
                            page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=15000)
                            time.sleep(3)
                        if is_logged_in():
                            print("  ✅ ログイン検知!")
                            break
                except Exception:
                    pass
            else:
                print("⏰ タイムアウト: ログインが完了しませんでした")
                if _is_unattended():
                    # 無人モードではハングせず例外で抜ける（呼び出し側が Discord 通知 + 失敗扱い）
                    try:
                        context.close()
                    except Exception:
                        pass
                    raise UnattendedLoginRequired(
                        "SUNO のブラウザログインが必要です。手動でログインを完了させてください。"
                        " 一度ログインすれば ~/.flow-playwright-profile に保存され、以降の自動実行は通ります。"
                    )
                print("ブラウザは開いたままにします。手動で操作してください。")
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
                context.close()
                return
            time.sleep(3)

        # /create ページに確実に移動
        if "/create" not in page.url:
            page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=15000)
            time.sleep(3)

        # ワークスペース確保（/create 上で名前を設定）
        workspace_name = settings.get("workspace") or ""
        if workspace_name:
            ensure_workspace(page, workspace_name)

        print(f"ページ読み込み完了: {page.url}\n")

        # ─── バッチモード: Claude CLI を 1 回だけ呼んで N 曲分を事前生成 ───
        # 事前生成済み（--songs-file / settings["pregenerated_songs"]）があれば LLM 生成をスキップしてそのまま投入
        batch_songs = None
        pregenerated = settings.get("pregenerated_songs")
        if pregenerated:
            batch_songs = pregenerated
            print(f"🎯 事前生成済み {len(batch_songs)} 曲をそのまま投入します（LLM生成スキップ）")
        elif settings.get("batch_mode") and settings.get("provider") in ("claude", "codex"):
            try:
                print(f"🎯 バッチモード: {settings.get('provider')} CLI でまとめて生成します")
                batch_songs = generate_content_batch(settings, loop_count)
                if len(batch_songs) < loop_count:
                    print(f"  ⚠️ 要求 {loop_count}曲 / 取得 {len(batch_songs)}曲。不足分はスキップ")
            except Exception as e:
                print(f"  ⚠️ バッチ生成失敗、都度生成にフォールバック: {e}")
                batch_songs = None

        # ─── ループ生成 ───
        for i in range(1, loop_count + 1):
            print(f"{'='*50}")
            print(f"  曲 {i}/{loop_count}")
            print(f"{'='*50}")
            _set_status(page, f"楽曲 {i}/{loop_count} を生成中...", "info")

            # 1. コンテンツ決定: バッチ生成済みがあれば使う、なければ都度生成
            if batch_songs is not None:
                if i - 1 >= len(batch_songs):
                    print(f"  ⏭ バッチ分を使い切ったためスキップ")
                    break
                content = batch_songs[i - 1]
                print(f"  📋 バッチから使用: {content.get('title', '')}")
                print(f"  🎨 スタイル: {content.get('styles', '')[:80]}...")
                if content.get("lyrics"):
                    print(f"  🎵 歌詞: {content['lyrics'][:50]}...")
            else:
                try:
                    content = generate_content(settings)
                except Exception as e:
                    print(f"  ❌ LLM API エラー: {e}")
                    if i < loop_count:
                        print(f"  30秒後にリトライ...")
                        time.sleep(30)
                    continue

            # 2. SUNO フォームに入力
            try:
                inject_into_suno(page, content)
            except Exception as e:
                print(f"  ❌ フォーム入力エラー: {e}")
                continue

            # 3. Create ボタンをクリック（著作権エラー時は歌詞をサニタイズして最大2回リトライ）
            create_ok = False
            for attempt in range(3):
                try:
                    click_create_button(page)
                except Exception as e:
                    print(f"  ❌ Create ボタンが見つかりません: {e}")
                    break

                # 3-a. Bot チャレンジ検出
                if detect_bot_challenge(page):
                    brand = (_load_dashboard_config_for_brand().get("channel_name") or "SUNO")
                    msg = f"🤖 [{brand}] Bot 判定が表示されました（楽曲 {i}/{loop_count}）。手動で解除してください。"
                    print(f"\n  {msg}")
                    _set_status(page, "Bot 判定を検出。手動操作が必要です", "err")
                    _notify_discord(msg)

                    # APP_KEEP_BROWSER=1 なら polling で解除を待ってからリトライ
                    if os.environ.get("APP_KEEP_BROWSER", "").strip() in ("1", "true", "yes"):
                        print(f"  ⏳ APP_KEEP_BROWSER=1: 最大10分、5秒間隔で解除を待機します（SUNO 画面で CAPTCHA を解除してください）")
                        deadline = time.time() + 600
                        resolved = False
                        while time.time() < deadline:
                            time.sleep(5)
                            try:
                                if not detect_bot_challenge(page):
                                    resolved = True
                                    break
                            except Exception:
                                pass
                        if resolved:
                            print(f"  ✅ Bot 判定が解除されました。続行します")
                            _set_status(page, "Bot 判定解除を確認、続行します", "ok")
                            try:
                                inject_into_suno(page, content)
                            except Exception as e:
                                print(f"  ⚠️ 再注入エラー: {e}")
                            continue
                        print(f"  ❌ 10分以内に解除されませんでした")

                    raise BotChallengeDetected(msg)

                if not detect_copyright_error(page):
                    print(f"  ✅ Create ボタンをクリックしました")
                    create_ok = True
                    break

                print(f"  ⚠️ SUNO 著作権フィルタに拒否されました (attempt {attempt+1}/3)")
                dismiss_error_toasts(page)

                # 歌詞を段階的にサニタイズして再投入
                if attempt == 0 and content.get("lyrics"):
                    sanitized = sanitize_lyrics_for_suno(content["lyrics"])
                    if sanitized != content["lyrics"]:
                        print(f"  🔧 歌詞をブラケット構造のみにサニタイズして再投入")
                        content["lyrics"] = sanitized
                        inject_into_suno(page, content)
                        continue
                if attempt == 1:
                    print(f"  🔧 フォールバック歌詞で再投入")
                    content["lyrics"] = _FALLBACK_BRACKET_LYRICS
                    inject_into_suno(page, content)
                    continue
                # 3回目で諦める
                break

            if not create_ok:
                print(f"  ❌ 楽曲 {i} の生成をスキップします")
                continue

            # 4. 次のループまで待機（±30% のゆらぎを加えて自動化検知を回避）
            if i < loop_count:
                # Cloudflare Bot 判定回避の最低保証（vol.6 で 15s 連投が弾かれた実績あり）
                # 環境変数 APP_SUNO_MIN_WAIT_SEC で上書き可能
                try:
                    min_wait = max(30, int(os.environ.get("APP_SUNO_MIN_WAIT_SEC", "60")))
                except Exception:
                    min_wait = 60
                # バッチモードは LLM 待ちが無いので素のままだと短いが、Bot 判定回避のため下限を挟む
                if batch_songs:
                    base_wait = max(min_wait, min(loop_interval, max(15, loop_interval // 3)))
                else:
                    base_wait = max(min_wait, loop_interval)
                jitter = random.uniform(-0.3, 0.3)
                wait_sec = max(min_wait, int(base_wait * (1.0 + jitter)))
                print(f"\n  ⏳ 次の生成まで {wait_sec} 秒待機中... (base={base_wait}s, jitter={jitter:+.0%}, min={min_wait}s)")
                for remaining in range(wait_sec, 0, -30):
                    print(f"    残り {remaining} 秒...")
                    time.sleep(min(30, remaining))

        print(f"\n{'='*50}")
        print(f"  完了: {loop_count} 曲の生成リクエストを送信しました")
        print(f"{'='*50}")

        # ブラウザを閉じるか確認
        # 無人モード（pipeline 経由・stdin 非 TTY）→ 即時 close
        # 対話モード → 10 秒だけ待つ。ハングしないよう必ず close に到達させる。
        # APP_KEEP_BROWSER=1 を立てた場合のみ、明示的にハング許容。
        try:
            if _is_unattended():
                print("\n無人モード: ブラウザを閉じます...")
            elif os.environ.get("APP_KEEP_BROWSER", "").strip() in ("1", "true", "yes"):
                print("\nAPP_KEEP_BROWSER=1: Ctrl+C まで開いたまま保持します。")
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
            else:
                print("\nブラウザは 10 秒後に閉じます（即時終了したい場合 Ctrl+C）...")
                try:
                    time.sleep(10)
                except KeyboardInterrupt:
                    pass
        finally:
            try:
                context.close()
            except Exception:
                pass


def _ensure_custom_mode(page):
    """SUNO の Create フォームを Custom モードにする。

    Simple モードでは title/styles/lyrics 欄が DOM に存在せず、注入が黙って失敗する。
    対象フィールドが未検出なら "Custom" トグルをクリックして切り替える。
    既に Custom（=フィールドが存在）なら何もしない。トグル未検出でも致命的にはしない。
    """
    def _fields_present():
        try:
            return page.evaluate("""() => {
                const hasTitle = !!document.querySelector('input[placeholder*="Title" i]');
                const hasLyrics = !!document.querySelector('textarea[placeholder*="lyrics" i]');
                const hasStyles = Array.from(document.querySelectorAll('div'))
                    .some(d => d.children.length === 0 && d.textContent.trim() === 'Styles');
                return hasTitle || hasLyrics || hasStyles;
            }""")
        except Exception:
            return False

    if _fields_present():
        return True

    print("  ⚙️ Custom モードへ切り替えを試行（title/styles/lyrics 欄が未検出）")
    # 1) Playwright のテキスト完全一致でトグルをクリック
    for label in ("Custom", "カスタム"):
        try:
            el = page.get_by_text(label, exact=True).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=2000)
                time.sleep(1.2)
                if _fields_present():
                    print("  ✓ Custom モードに切り替えました")
                    return True
        except Exception:
            pass
    # 2) JS で "Custom" 要素/トグルを走査クリック（aria-selected 済みは除外）
    try:
        clicked = page.evaluate("""() => {
            const cand = Array.from(document.querySelectorAll('button, [role="tab"], [role="switch"], div, span, label'))
                .filter(e => {
                    const t = (e.textContent || '').trim();
                    return t === 'Custom' || t === 'カスタム';
                });
            for (const e of cand) {
                if (e.getAttribute && e.getAttribute('aria-selected') === 'true') continue;
                e.click();
                return true;
            }
            return false;
        }""")
        if clicked:
            time.sleep(1.2)
            if _fields_present():
                print("  ✓ Custom モードに切り替えました (JS)")
                return True
    except Exception:
        pass
    print("  ⚠️ Custom トグルが見つかりませんでした（既に Custom か、UI 変更の可能性）")
    return _fields_present()


def inject_into_suno(page, content):
    """SUNO のフォームに値を注入 — Ghost Writer (Tampermonkey) と完全同一のロジック。

    Ghost Writer の setReactValue + セレクタをそのまま page.evaluate で実行する。
    ブラウザコンテキスト内で直接 React の _valueTracker をリセットするため、
    Playwright の fill() より確実に React に値が反映される。
    """
    mode = content["mode"]
    title = content.get("title", "")
    styles = content.get("styles", "")
    lyrics = content.get("lyrics", "")

    # Simple モード対策: 入力欄が存在しなければ Custom へ切り替える
    _ensure_custom_mode(page)

    # Ghost Writer と完全同一の JS を page.evaluate で実行
    results = page.evaluate("""(args) => {
        const { title, styles, lyrics, mode } = args;

        // ── Ghost Writer の setReactValue（そのまま移植）──
        function setReactValue(el, value, isTextarea) {
            // 1) 直接 value セット
            el.value = value;
            // 2) React の _valueTracker をリセット
            const tracker = el._valueTracker;
            if (tracker) tracker.setValue("");
            // 3) native setter で書き込み（React 制御コンポーネント対応）
            const proto = isTextarea
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
            if (descriptor && descriptor.set) descriptor.set.call(el, value);
            // 4) input イベント発火
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }

        const results = { title: false, styles: false, lyrics: false, debug: {} };

        // ── タイトル: Ghost Writer と同一セレクタ ──
        if (title) {
            const input = document.querySelector('input[placeholder="Song Title (Optional)"]');
            if (input) {
                setReactValue(input, title, false);
                results.title = true;
            } else {
                // フォールバック: placeholder 部分一致
                const fallback = document.querySelector('input[placeholder*="Title"]')
                              || document.querySelector('input[placeholder*="title"]');
                if (fallback) { setReactValue(fallback, title, false); results.title = true; }
                results.debug.title_inputs = Array.from(document.querySelectorAll('input')).map(i =>
                    (i.placeholder || '').slice(0, 40)).filter(Boolean);
            }
        }

        // ── スタイル: Ghost Writer と同一セレクタ ──
        if (styles) {
            // Ghost Writer: "Styles" div → 4階層上 → textarea
            const divs = Array.from(document.querySelectorAll('div'));
            const stylesDiv = divs.find(div =>
                div.textContent.trim() === "Styles" && div.children.length === 0
            );
            if (stylesDiv) {
                // 4階層固定ではなく 1〜6階層を探索（DOM 変更耐性）
                let found = false;
                let el = stylesDiv;
                for (let i = 0; i < 6 && el; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    const ta = el.querySelector('textarea');
                    if (ta) {
                        setReactValue(ta, styles, true);
                        results.styles = true;
                        found = true;
                        break;
                    }
                }
                if (!found) results.debug.styles_div_found_but_no_textarea = true;
            } else {
                // フォールバック: placeholder で探す
                const fb = document.querySelector('textarea[placeholder*="style" i]')
                        || document.querySelector('textarea[placeholder*="Describe" i]');
                if (fb) {
                    setReactValue(fb, styles, true);
                    results.styles = true;
                }
                results.debug.styles_divs = divs
                    .filter(d => d.children.length === 0 && (d.textContent || '').trim().length < 30)
                    .map(d => (d.textContent || '').trim())
                    .filter(Boolean)
                    .slice(0, 20);
            }
        }

        // ── 歌詞: 複数戦略で textarea を探索（Ghost Writer findLyricsTextarea 移植）──
        if (lyrics && mode !== "styles_title_only") {
            let textarea = null;
            // 1) placeholder ベース（文言バリエーション対応・大小無視・後方互換）
            const lyricSelectors = [
                'textarea[placeholder*="Write some lyrics" i]',
                'textarea[placeholder*="Add your own lyrics" i]',
                'textarea[placeholder*="own lyrics" i]',
                'textarea[placeholder*="lyrics" i]',
                'textarea[placeholder*="歌詞"]'
            ];
            for (const sel of lyricSelectors) {
                const el = document.querySelector(sel);
                if (el) { textarea = el; break; }
            }
            // 2) "Lyrics" ラベルから祖先方向（最大8階層）の textarea を辿る
            if (!textarea) {
                const labelEl = Array.from(document.querySelectorAll('div, span, label'))
                    .find(d => d.children.length === 0 && d.textContent.trim() === "Lyrics");
                if (labelEl) {
                    let p = labelEl;
                    for (let i = 0; i < 8 && p; i++) {
                        p = p.parentElement;
                        const ta = p && p.querySelector('textarea');
                        if (ta) { textarea = ta; break; }
                    }
                }
            }
            if (textarea) {
                setReactValue(textarea, lyrics, true);
                results.lyrics = true;
            } else {
                results.debug.lyrics_textareas = Array.from(document.querySelectorAll('textarea'))
                    .map(t => (t.placeholder || '').slice(0, 40)).filter(Boolean).slice(0, 10);
            }
        }

        return results;
    }""", {
        "title": title,
        "styles": styles,
        "lyrics": lyrics,
        "mode": mode,
    })

    if title:
        print(f"  📝 タイトル: {'✅' if results.get('title') else '❌'} {title}")
        if not results.get('title') and results.get('debug', {}).get('title_inputs'):
            print(f"     ⚠️ 見つかった input placeholder: {results['debug']['title_inputs']}")
    if styles:
        print(f"  🎨 スタイル: {'✅' if results.get('styles') else '❌'} {styles[:60]}...")
        if not results.get('styles'):
            dbg = results.get('debug', {})
            if dbg.get('styles_div_found_but_no_textarea'):
                print(f"     ⚠️ 'Styles' div は見つかったが textarea が見つかりません")
            elif dbg.get('styles_divs'):
                print(f"     ⚠️ 'Styles' div が見つかりません。空div一覧: {dbg['styles_divs'][:10]}")
    if lyrics and mode != "styles_title_only":
        print(f"  🎵 歌詞: {'✅' if results.get('lyrics') else '❌'} {lyrics[:50]}...")
        if not results.get('lyrics') and results.get('debug', {}).get('lyrics_textareas'):
            print(f"     ⚠️ 歌詞 textarea が見つかりません。textarea placeholder 一覧: {results['debug']['lyrics_textareas']}")


_BRACKET_LINE_RE = re.compile(r'^\s*\[[^\]]+\]\s*$')

_FALLBACK_BRACKET_LYRICS = (
    "[Intro - soft piano]\n"
    "[Verse - warm bass and brushed drums]\n"
    "[Chorus - full arrangement]\n"
    "[Bridge - stripped back]\n"
    "[Outro - gentle fade]"
)


def sanitize_lyrics_for_suno(lyrics: str) -> str:
    """Suno の著作権フィルタを避けるため、歌詞からブラケット構造行以外を除去する。

    Why: Suno の copyright filter は一般的な英語歌詞フレーズにも誤反応する（リサーチ済み）。
    ブラケット構造指示 `[Section - description]` のみは歌詞として扱われず安全。
    """
    if not lyrics:
        return _FALLBACK_BRACKET_LYRICS
    kept = [ln for ln in lyrics.splitlines() if _BRACKET_LINE_RE.match(ln)]
    if not kept:
        return _FALLBACK_BRACKET_LYRICS
    return "\n".join(kept)


def detect_bot_challenge(page) -> bool:
    """Cloudflare turnstile / hCaptcha / reCAPTCHA / "verify you are human" 系の Bot 判定 UI を検出。

    誤検知を抑えるための優先順位:
      1. iframe の src ホスト名 (challenges.cloudflare.com / hcaptcha.com / recaptcha) — 主軸
      2. challenge コンテナ要素 ([class*="cf-turnstile"], [id*="cf-challenge"], .h-captcha, .g-recaptcha, [role="dialog"]) 内の可視テキストに明示フレーズが含まれる場合
    body.innerText 全体への部分一致は SUNO のメニュー/フッタに含まれる "security check" 等で
    誤検知するため使わない。
    """
    # 緊急時の最終手段: APP_SKIP_BOT_CHECK=1 で検知をスキップ（誤検知が再発した際の avoidance）
    if os.environ.get("APP_SKIP_BOT_CHECK", "").strip() in ("1", "true", "yes"):
        print("  ⚠️ APP_SKIP_BOT_CHECK=1 により Bot 判定検知をスキップしています（緊急バイパス）")
        return False
    try:
        return bool(page.evaluate("""() => {
            // --- (1) iframe ホスト名チェック（主軸）---
            const iframes = Array.from(document.querySelectorAll('iframe'));
            const hostHints = ['challenges.cloudflare.com', 'hcaptcha.com', 'recaptcha'];
            for (const f of iframes) {
                const src = (f.src || '').toLowerCase();
                if (!hostHints.some(h => src.includes(h))) continue;
                // 可視性確認: display:none や 0x0 の隠し iframe は無視
                const rect = f.getBoundingClientRect();
                const style = window.getComputedStyle(f);
                const visible = rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
                if (visible) return true;
            }

            // --- (2) challenge コンテナ内の明示フレーズ ---
            const containers = document.querySelectorAll(
                '[class*="cf-turnstile"], [id*="cf-challenge"], [id*="cf-wrapper"], '
                + '.h-captcha, .g-recaptcha, [class*="challenge-container"], '
                + '[role="dialog"][aria-modal="true"]'
            );
            // 厳しめのフレーズ（単語境界要求）。"security check" は誤検知が多いので外す
            const phrasePatterns = [
                /\\bverify(ing)?\\s+you\\s+are\\s+human\\b/i,
                /\\bare\\s+you\\s+a\\s+robot\\b/i,
                /\\bunusual\\s+traffic\\b/i,
                /\\bchecking\\s+if\\s+the\\s+site\\s+connection\\s+is\\s+secure\\b/i,
                /\\bplease\\s+complete\\s+the\\s+security\\s+check\\b/i,
            ];
            for (const el of containers) {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
                if (!visible) continue;
                const text = (el.innerText || '');
                if (phrasePatterns.some(re => re.test(text))) return true;
            }
            return false;
        }"""))
    except Exception:
        return False


def detect_copyright_error(page, timeout_sec=5):
    """Create 後の数秒以内に SUNO の著作権エラートーストが表示されたかを判定する。

    "Couldn't generate that. Your lyrics contain copyrighted material." を検出。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            found = page.evaluate("""() => {
                const text = (document.body.innerText || '').toLowerCase();
                return (text.includes("couldn't generate that") || text.includes("cannot generate"))
                    && (text.includes("copyrighted") || text.includes("copyright"));
            }""")
            if found:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def dismiss_error_toasts(page):
    """エラートーストの × ボタンを全てクリックして閉じる"""
    try:
        page.evaluate("""() => {
            const btns = document.querySelectorAll('button[aria-label*="close" i], button[aria-label*="dismiss" i]');
            btns.forEach(b => { try { b.click(); } catch(e) {} });
        }""")
    except Exception:
        pass


def click_create_button(page):
    """Create ボタンをクリック（複数戦略）"""
    time.sleep(1)

    # 戦略1: Playwright get_by_role
    for name in ["Create", "create", "作成"]:
        try:
            btn = page.get_by_role("button", name=name).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                return
        except Exception:
            continue

    # 戦略2: テキストマッチ（大小無視）
    try:
        btn = page.locator('button:has-text("Create")').first
        if btn.count() > 0 and btn.is_visible():
            btn.click(timeout=3000)
            return
    except Exception:
        pass

    # 戦略3: data-testid
    try:
        btn = page.locator('button[data-testid="create-button"]').first
        if btn.count() > 0:
            btn.click(timeout=3000)
            return
    except Exception:
        pass

    # 戦略4: JS で全ボタンを走査
    try:
        clicked = page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const t = (b.textContent || '').trim().toLowerCase();
                if (t === 'create' || t === '作成') {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            return
    except Exception:
        pass

    raise Exception("Create ボタンが見つかりません")


# ─── 設定管理 ──────────────────────────────────────────

def _run_download_only(workspace_name, target_dir, settings):
    """Playwright を起動して指定 Workspace の楽曲をダウンロードして終了"""
    from playwright.sync_api import sync_playwright
    profile_dir = str(Path.home() / ".config/orzz/chromium_profile")
    with sync_playwright() as p:
        launch_kwargs = dict(
            user_data_dir=profile_dir, headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"],
            accept_downloads=True,
        )
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        # SUNO SPA の内部 fetch/XHR をインターセプトして audio_url をキャッシュ
        context.add_init_script(_SUNO_AUDIO_URL_INTERCEPTOR)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            download_workspace_tracks(page, workspace_name, target_dir)
        finally:
            time.sleep(2)
            context.close()


# ブラウザ右下に Automation Studio のステータスを表示するオーバーレイ。
# `window.__orzzStatus(msg, variant?)` を呼ぶと反映される。
_STATUS_OVERLAY_SCRIPT = r"""
(() => {
    if (window.__orzzStatusInstalled) return;
    window.__orzzStatusInstalled = true;
    function ensurePanel() {
        let el = document.getElementById('__orzz_status_panel');
        if (!el) {
            el = document.createElement('div');
            el.id = '__orzz_status_panel';
            el.style.cssText = [
                'position:fixed', 'bottom:16px', 'right:16px', 'z-index:2147483647',
                'background:rgba(10,10,10,0.92)', 'color:#f1f1f1',
                'padding:12px 16px', 'border-radius:10px',
                'border:1px solid rgba(62,166,255,0.55)',
                'box-shadow:0 8px 24px rgba(0,0,0,0.45)',
                'font: 12px/1.5 -apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif',
                'max-width:380px', 'pointer-events:none'
            ].join(';');
            const title = document.createElement('div');
            title.textContent = '🤖 ' + (window.__appBrandLabel || 'Automation Studio') + ' 自動操作中';
            title.style.cssText = 'font-weight:700;color:#3ea6ff;margin-bottom:4px;';
            const body = document.createElement('div');
            body.id = '__orzz_status_body';
            body.style.cssText = 'white-space:pre-wrap;word-break:break-word;';
            const time = document.createElement('div');
            time.id = '__orzz_status_time';
            time.style.cssText = 'font-size:10px;color:#888;margin-top:6px;font-family:monospace;';
            el.appendChild(title); el.appendChild(body); el.appendChild(time);
            (document.body || document.documentElement).appendChild(el);
        }
        return el;
    }
    window.__orzzStatus = function(msg, variant) {
        try {
            const el = ensurePanel();
            const body = el.querySelector('#__orzz_status_body');
            const time = el.querySelector('#__orzz_status_time');
            body.textContent = String(msg || '').slice(0, 500);
            const colors = {info:'#3ea6ff', ok:'#22c55e', warn:'#f59e0b', err:'#ef4444'};
            el.style.borderColor = colors[variant] || colors.info;
            if (time) time.textContent = new Date().toLocaleTimeString('ja-JP');
        } catch (e) {}
    };
    // ブランド非依存の alias（v2 配布化で推奨される命名）
    window.__appStatus = window.__orzzStatus;
    // 初期表示
    document.addEventListener('DOMContentLoaded', () => window.__orzzStatus('起動準備中...', 'info'));
})();
"""


def _set_status(page, message, variant="info"):
    """ブラウザ上のオーバーレイにステータスを表示（失敗しても例外は出さない）"""
    try:
        page.evaluate("(args) => window.__orzzStatus && window.__orzzStatus(args[0], args[1])",
                      [message, variant])
    except Exception:
        pass


# SUNO SPA が送出する /api/feed や /api/clips のレスポンスを横取りして
# window.__sunoAudioUrlCache (id → {audioUrl, title}) に溜め込むインターセプタ。
# context.add_init_script で document-start 時点に注入する必要がある。
_SUNO_AUDIO_URL_INTERCEPTOR = r"""
(() => {
    if (window.__sunoAudioInterceptorInstalled) return;
    window.__sunoAudioInterceptorInstalled = true;
    window.__sunoAudioUrlCache = window.__sunoAudioUrlCache || {};
    const cache = window.__sunoAudioUrlCache;

    function parseAndCache(data) {
        try {
            const clips = Array.isArray(data) ? data
                : Array.isArray(data && data.clips) ? data.clips
                : Array.isArray(data && data.data) ? data.data
                : (data && data.id) ? [data]
                : [];
            clips.forEach(clip => {
                const id = clip && (clip.id || clip.clip_id);
                const url = clip && (clip.audio_url || clip.audio || clip.file_url || clip.mp3_url);
                if (id && url) {
                    cache[id] = {
                        audioUrl: url,
                        title: clip.title || clip.name || id
                    };
                }
            });
        } catch (e) { /* noop */ }
    }
    const isApiUrl = (url) => typeof url === 'string' && /\/api\/(feed|clips|v2\/(feed|clips))/.test(url);

    // fetch をラップ
    const origFetch = window.fetch;
    if (origFetch) {
        window.fetch = async function(...args) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
            const result = await origFetch.apply(this, args);
            if (isApiUrl(url)) {
                try { result.clone().json().then(parseAndCache).catch(() => {}); } catch(e) {}
            }
            return result;
        };
    }

    // XMLHttpRequest をラップ
    const origOpen = window.XMLHttpRequest.prototype.open;
    const origSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__sunoUrl = url;
        return origOpen.apply(this, [method, url, ...rest]);
    };
    window.XMLHttpRequest.prototype.send = function(...args) {
        if (isApiUrl(this.__sunoUrl)) {
            this.addEventListener('load', function() {
                try { parseAndCache(JSON.parse(this.responseText)); } catch(e) {}
            });
        }
        return origSend.apply(this, args);
    };
})();
"""


def load_config(path=None):
    """設定ファイルを読み込み"""
    config = DEFAULT_SETTINGS.copy()
    config_path = Path(path) if path else DEFAULT_CONFIG

    if config_path.exists():
        with open(config_path) as f:
            saved = json.load(f)
        config.update(saved)
    return config


def save_config(config, path=None):
    """設定ファイルを保存"""
    config_path = Path(path) if path else DEFAULT_CONFIG
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"設定を保存しました: {config_path}")


# ─── メイン ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="orzz. SUNO 自動楽曲生成")
    parser.add_argument("--config", "-c", help="設定ファイルのパス")
    parser.add_argument("--prompt", "-p", help="生成プロンプト")
    parser.add_argument("--count", "-n", type=int, help="生成回数")
    parser.add_argument("--interval", "-i", type=int, help="ループ間隔（秒）")
    parser.add_argument("--provider", choices=["gemini", "chatgpt", "claude", "codex"], help="AIプロバイダー")
    parser.add_argument("--model", "-m", help="モデル名")
    parser.add_argument("--api-key", "-k", help="APIキー")
    parser.add_argument("--mode", choices=["lyrics", "lyrics_styles", "styles_title_only", "instrumental_filler"],
                        help="生成モード（instrumental_filler: lyrics を [instrumental] x 5000文字で固定充填）")
    parser.add_argument("--headless", action="store_true", help="ヘッドレスモード")
    parser.add_argument("--workspace", "-w", help="SUNO Workspace 名（例: orzz_vol74）。指定時は確保してから生成")
    parser.add_argument("--download-workspace", help="指定 Workspace の楽曲を一括ダウンロード（生成せず）")
    parser.add_argument("--download-dir", help="ダウンロード先フォルダ（--download-workspace と併用）")
    parser.add_argument("--batch", action="store_true", help="CLI 一括生成モード（N曲分を1回で生成）")
    parser.add_argument("--diversity-threshold", type=float, default=None,
                        help="類似度しきい値 0.0〜1.0（既定 0.6、1.0で実質無効）")
    parser.add_argument("--diversity-retry", type=int, default=None,
                        help="似すぎたときの再生成上限 0〜5（既定 2、0で再生成しない）")
    parser.add_argument("--history-limit", type=int, default=None,
                        help="チャンネル別の曲履歴保持件数 0〜200（既定 40、0で履歴・類似回避を無効化）")
    parser.add_argument("--save-config", action="store_true", help="現在の設定を保存して終了")
    parser.add_argument("--songs-file", help="事前生成済み楽曲JSON（[{title,styles,lyrics},...] または {\"songs\":[...]}）をそのまま投入。LLM生成をスキップし、合意済みドラフトを確実に使う")
    args = parser.parse_args()

    # 設定読み込み
    settings = load_config(args.config)

    # コマンドライン引数で上書き
    if args.prompt:
        settings["prompt"] = args.prompt
    if args.count:
        settings["loop_count"] = args.count
    if args.interval:
        settings["loop_interval_sec"] = args.interval
    if args.provider:
        settings["provider"] = args.provider
    if args.model:
        settings["model"] = args.model
    if args.api_key:
        settings["api_key"] = args.api_key
    if args.mode:
        settings["generation_mode"] = args.mode
    if args.headless:
        settings["headless"] = True
    if args.workspace:
        settings["workspace"] = args.workspace
    if args.batch:
        settings["batch_mode"] = True
    if args.diversity_threshold is not None:
        settings["diversity_threshold"] = args.diversity_threshold
    if args.diversity_retry is not None:
        settings["diversity_retry"] = args.diversity_retry
    if args.history_limit is not None:
        settings["history_limit"] = args.history_limit
    if args.songs_file:
        _pre = json.loads(Path(args.songs_file).read_text(encoding="utf-8"))
        if isinstance(_pre, dict) and "songs" in _pre:
            _pre = _pre["songs"]
        if not isinstance(_pre, list) or not _pre:
            print(f"❌ --songs-file の中身が空か不正です: {args.songs_file}")
            sys.exit(1)
        settings["pregenerated_songs"] = _pre
        settings["loop_count"] = len(_pre)

    # 設定保存モード
    if args.save_config:
        save_config(settings, args.config)
        return

    # ダウンロードモード（生成はせず、指定 Workspace の楽曲を一括DL）
    if args.download_workspace:
        target_dir = args.download_dir or str(Path.cwd() / "suno_downloads")
        print("=" * 50)
        print("  orzz. SUNO 楽曲ダウンロード")
        print("=" * 50)
        print(f"  Workspace: {args.download_workspace}")
        print(f"  保存先: {target_dir}")
        print("=" * 50)
        _run_download_only(args.download_workspace, target_dir, settings)
        return

    # APIキー確認（CLI プロバイダーは CLI 経由なので不要）
    if settings.get("provider") not in ("claude", "codex") and not settings.get("api_key"):
        print("❌ APIキーが設定されていません。")
        print(f"\n以下のいずれかで設定してください:")
        print(f"  1. {DEFAULT_CONFIG} に api_key を記入")
        print(f"  2. --api-key オプションで指定")
        print(f"  3. --save-config で設定ファイルを生成")
        print(f"  （provider=claude/codex の場合は CLI を使用するためAPIキー不要）")
        sys.exit(1)

    # CLI の存在確認
    if settings.get("provider") in ("claude", "codex"):
        import shutil
        cli_cmd = settings.get("codex_cli", "codex") if settings.get("provider") == "codex" else settings.get("claude_cli", "claude")
        if not shutil.which(cli_cmd):
            print(f"❌ {settings.get('provider')} CLI が PATH 上に見つかりません: '{cli_cmd}'")
            sys.exit(1)

    # 実行情報表示
    print("=" * 50)
    print("  orzz. SUNO 自動楽曲生成")
    print("=" * 50)
    print(f"  プロバイダー: {settings['provider']}")
    print(f"  モデル: {settings['model']}")
    print(f"  生成モード: {settings['generation_mode']}")
    print(f"  生成回数: {settings['loop_count']}")
    print(f"  ループ間隔: {settings['loop_interval_sec']}秒")
    if settings.get("workspace"):
        print(f"  Workspace: {settings['workspace']}")
    print(f"  プロンプト: {settings['prompt'][:60]}...")
    print("=" * 50)

    # ブラウザ自動操作開始
    run_browser_automation(settings)


if __name__ == "__main__":
    try:
        main()
    except UnattendedLoginRequired as e:
        # app_pipeline.py が exit code 75 を「ログイン要」サインとして検知する
        # （sentinel は stderr ではなく stdout に出力。pipeline は stdout を tail する）
        print(f"\n[UNATTENDED_LOGIN_REQUIRED] {e}", flush=True)
        sys.exit(75)
    except BotChallengeDetected as e:
        # Bot 判定は手動介入が必要なので unattended_login と同じ exit 75 で扱う
        print(f"\n[BOT_CHALLENGE_DETECTED] {e}", flush=True)
        sys.exit(75)
