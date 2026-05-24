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
  "styles": "<comma-separated English Suno styles INCLUDING song structure>",
  "lyrics": "<bracket-only structural notation — NO SUNG LYRICS>"
}
Rules:
- `styles` must describe genre, BPM, instruments, and structural hints.
  e.g. `jazz house, 120 BPM, four-on-the-floor kick, deep sub bass, 0-5s piano intro, gradual instrument layering, 30s+ full mix, outro fade`.
- `lyrics` — THIS IS AN INSTRUMENTAL BGM TRACK. Output ONLY bracketed section directives, one per line. DO NOT write any sung words, verses, choruses, rhymes, or sentences.
  Required format — each line is a single `[Section - instrumental description]` bracket:
      [Intro - soft piano and muted trumpet]
      [Verse - walking bass and brushed snare]
      [Chorus - full arrangement with warm strings]
      [Bridge - stripped back to piano and rhodes]
      [Outro - gentle fade with reverb tail]
  NEVER output lines outside brackets. NEVER include quoted phrases, pronouns ("I", "you", "we"), emotive words ("love", "heart", "night", "forever"), or any text resembling real song lyrics — SUNO's copyright filter rejects them as false positives.
- Output ONLY the JSON object. No prose before or after.""" + SUNO_SAFETY_CLAUSE

APPEND_PROMPT_JSON_LYRICS = """

【Output Format — JSON ONLY】
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{
  "title": "<song title>",
  "lyrics": "<bracket-only structural notation — NO SUNG LYRICS>"
}
Rules:
- `lyrics` — THIS IS AN INSTRUMENTAL BGM TRACK. Output ONLY bracketed section directives, one per line. DO NOT write sung words.
  Required format:
      [Intro - piano solo]
      [Buildup - percussion layers in]
      [Main - full arrangement]
      [Outro - fade out]
  NEVER output lines outside brackets. NEVER include quoted phrases, pronouns, or emotive words — SUNO's copyright filter triggers false positives on common lyric-like phrases.
- Output ONLY the JSON object. No prose before or after.""" + SUNO_SAFETY_CLAUSE


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
    """claude CLI をターミナル経由で呼び出しテキスト生成（API不使用）

    `claude -p "<prompt>"` の stdout を受け取る。
    各ループで都度起動し、JSONを 1件返させる（考案 → 採用の繰り返し）。
    """
    import shutil
    import subprocess

    # CLI の存在確認
    cli_path = shutil.which(cli_cmd) or cli_cmd
    try:
        proc = subprocess.run(
            [cli_path, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise Exception(
            f"claude CLI が見つかりません: '{cli_cmd}'. "
            f"Claude Code CLI をインストールしてください。"
        )
    except subprocess.TimeoutExpired:
        raise Exception(f"claude CLI タイムアウト ({timeout}s)")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise Exception(f"claude CLI エラー (rc={proc.returncode}): {err}")

    return proc.stdout or ""


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
        mode_hint, mode_rule = "lyrics_styles", "include `lyrics` with Suno section tags [Intro], [Verse], [Chorus], [Outro] or bracket structural notes."
    else:
        mode_hint, mode_rule = "lyrics", "include `lyrics` with Suno section tags. Omit `styles` if strict."

    full_prompt = base_prompt + APPEND_PROMPT_JSON_BATCH.format(
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
    songs = []
    instrumental_filler = (mode == "instrumental_filler")
    instrumental_lyrics = build_instrumental_filler() if instrumental_filler else ""
    for s in obj["songs"][:count]:
        # instrumental_filler: AI 出力の lyrics は捨て、固定の [instrumental] 充填で上書き
        lyrics_value = instrumental_lyrics if instrumental_filler else str(s.get("lyrics", "")).strip()
        songs.append({
            "title": str(s.get("title", "")).strip(),
            "styles": str(s.get("styles", "")).strip(),
            "lyrics": lyrics_value,
            "mode": mode,
        })
    if instrumental_filler:
        print(f"  ✅ {len(songs)}曲分のメタデータを取得（lyrics は [instrumental] x {INSTRUMENTAL_TARGET_CHARS}文字 で充填）")
    else:
        print(f"  ✅ {len(songs)}曲分のメタデータを取得しました")
    return songs


def generate_content(settings):
    """LLM / Claude CLI でタイトル・スタイル・歌詞を生成"""
    mode = settings["generation_mode"]
    base_prompt = settings["prompt"]
    provider = settings["provider"]

    # instrumental_filler は AI には styles_title_only として依頼し、lyrics は機械生成で上書き
    effective_mode = "styles_title_only" if mode == "instrumental_filler" else mode

    # CLI provider は JSON 出力用プロンプトを使用
    if provider in ("claude", "codex"):
        if effective_mode == "styles_title_only":
            full_prompt = base_prompt + APPEND_PROMPT_JSON_STYLES_TITLE_ONLY
        elif effective_mode == "lyrics_styles":
            full_prompt = base_prompt + APPEND_PROMPT_JSON_LYRICS_STYLES
        else:
            full_prompt = base_prompt + APPEND_PROMPT_JSON_LYRICS
    else:
        if effective_mode == "styles_title_only":
            full_prompt = base_prompt + APPEND_PROMPT_STYLES_TITLE_ONLY
        elif effective_mode == "lyrics_styles":
            full_prompt = base_prompt + APPEND_PROMPT_WITH_STYLES
        else:
            full_prompt = base_prompt + APPEND_PROMPT_WITHOUT_STYLES

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
        print(f"  タイトル: {title}")
        print(f"  スタイル: {styles[:80]}...")
        print(f"  歌詞: [instrumental] x {INSTRUMENTAL_TARGET_CHARS}文字 で充填（{len(lyrics)}文字）")
    else:
        print(f"  タイトル: {title}")
        print(f"  スタイル: {styles[:80]}...")
        if lyrics:
            print(f"  歌詞: {lyrics[:50]}...")

    return {"title": title, "styles": styles, "lyrics": lyrics, "mode": mode}


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
    # DOM 上の workspace カードは <div role="button"> で text に "orzz_vol77 80 Songs · 1d ago" のような形
    # exact=False で部分一致検索する
    try:
        existing = page.locator('div[role="button"]').filter(has_text=workspace_name).first
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
        batch_songs = None
        if settings.get("batch_mode") and settings.get("provider") in ("claude", "codex"):
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

        // ── 歌詞: Ghost Writer と同一セレクタ ──
        if (lyrics && mode !== "styles_title_only") {
            const textarea = document.querySelector('textarea[placeholder*="Write some lyrics"]');
            if (textarea) {
                setReactValue(textarea, lyrics, true);
                results.lyrics = true;
            } else {
                const fb = document.querySelector('textarea[placeholder*="lyrics" i]')
                        || document.querySelector('textarea[placeholder*="歌詞"]');
                if (fb) { setReactValue(fb, lyrics, true); results.lyrics = true; }
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
    parser.add_argument("--save-config", action="store_true", help="現在の設定を保存して終了")
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
