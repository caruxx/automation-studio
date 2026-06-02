#!/usr/bin/env python3
"""Claude CLI を用いて YouTube タイトル・説明・タグを JSON 提案させる軽量モジュール。

API は使わず `claude -p "<prompt>"` を subprocess で呼び、単一 JSON オブジェクトを受領する。
他スクリプト (suno_auto_create.py) と同じ契約。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


DEFAULT_CLI = "claude"
DEFAULT_TIMEOUT = 180


def _run_claude(cli_cmd: str, prompt: str, timeout: int = DEFAULT_TIMEOUT,
                allow_read: bool = False, read_paths: list[Path] | None = None) -> str:
    cli_path = shutil.which(cli_cmd) or cli_cmd
    args = [cli_path, "-p", prompt]
    if allow_read:
        # Vision 入力: プロンプト内で Read ツール呼び出しを許可（サムネ画像読み取り用）
        args += ["--allowedTools", "Read"]
        seen: set[str] = set()
        for p in read_paths or []:
            parent = str(Path(p).parent)
            if parent and parent not in seen:
                args += ["--add-dir", parent]
                seen.add(parent)
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(f"claude CLI が見つかりません: '{cli_cmd}'")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI タイムアウト ({timeout}s)")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(f"claude CLI エラー (rc={proc.returncode}): {err}")
    return proc.stdout or ""


def _find_thumbnail(folder: Path) -> Optional[Path]:
    """動画フォルダ直下のサムネ画像を返す（サムネイル.jpg を最優先）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    for pat in ("サムネイル.jpg", "サムネイル.png", "thumbnail.jpg", "thumbnail.png", "vol*.jpg", "vol*.png", "vol*.jpeg"):
        for f in sorted(folder.glob(pat)):
            return f
    return None


def _extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = candidate[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# ─── プロンプトテンプレート ────────────────────────────────

_TITLES_PROMPT = """You are a viewer psychology expert crafting YouTube titles for "{channel_name}" — a BGM/instrumental music channel.

=== Channel Persona ===
{persona}

=== Video Info ===
Songs: {song_count} tracks
{songs}
Publish date: {publish_date}
Current title: {current_title}

=== Thumbnail (visual cue for the title) ==={thumbnail_section}

=== Benchmark Analysis (use as viewer-context, may be in Japanese) ===
{benchmark_section}

=== Your Task ===
Generate {count} English YouTube title candidates that make viewers CLICK and STAY.

Think from the VIEWER'S perspective:
- What are they searching for right now? (studying, working late, can't sleep, need focus, unwinding after a long day)
- What emotional state do they want to reach? (calm, focused, nostalgic, cozy, elegant)
- What scene do they imagine? (rainy cafe, midnight city, golden sunset, quiet library)

Title Psychology Rules:
- Output English only — even if the benchmark notes are in Japanese, titles MUST be English.
- Anchor each title in the benchmark's proven viewer needs, search keywords, and underserved niches above.
- Promise a clear viewer transformation: "from scattered to focused", "from awake at 3AM to gently settled", etc.
- Use the "Oh, this is exactly it" test: the title should name the viewer's moment more precisely than they could.
- Paint a scene the viewer wants to BE IN (not just hear).
- Use sensory words: warm, soft, velvet, golden, midnight, rain, breeze, glow
- Include a time/place/mood anchor: "for Late Night Work", "Rainy Day Cafe", "3AM Study Session"
- Keep under 60 chars — shorter titles get more clicks on mobile
- Don't use channel name, vol numbers, or generic "BGM" alone
- Each title should target a DIFFERENT viewer moment/need

Respond with a SINGLE JSON object, no markdown fences:
{{"titles": ["Title one", "Title two", ...]}}"""


_DESCRIPTION_PROMPT = """You are writing a YouTube description for "{channel_name}" that makes viewers feel understood.

=== Channel Persona ===
{persona}

=== Video ===
Title: {current_title}
Songs ({song_count} tracks):
{songs}
Publish date: {publish_date}

=== Thumbnail (visual scene for the opening line) ==={thumbnail_section}

=== Reference Style ===
{reference}

=== Benchmark Analysis (use as viewer-context, may be in Japanese) ===
{benchmark_section}

=== Your Task ===
Write an English description in TWO PARTS that connect emotionally with the viewer.
Use the benchmark notes above to ground the opening in a viewer moment that competing channels have already validated. Output stays English regardless of input language.

Part 1 — OPENING (2-4 lines):
- Speak directly to the viewer's current moment.
- Don't describe the music — describe how they'll FEEL.
- Examples: "The city quiets down. Your coffee is still warm. This is your time."
  "Can't sleep? Let these sounds carry you somewhere gentle."

Part 2 — CLOSING:
- A gentle invitation to subscribe.
- 10-15 hashtags mixing discovery tags (#StudyMusic #ChillVibes) with niche (#MidnightLounge #RainyDayBGM).

DO NOT write the tracklist yourself. The verified tracklist with real timecodes is inserted automatically between OPENING and CLOSING by a separate process. Any timestamps or song titles you write here will conflict with the real data and break the output.

Language: English only. Do not write Japanese. Avoid literal translation tone; write like native YouTube copy for the target viewer.
Marketing/copy guidance:
- Lead with the viewer's need, not the creator's process.
- Use concrete scenes, mental availability cues, and soft emotional contrast.
- Make the viewer feel, "this understands my evening / study session / quiet escape."

Respond with a SINGLE JSON object (no markdown fences):
{{"opening": "<opening lines, use \\n for newlines>", "closing": "<closing + hashtags, use \\n for newlines>"}}"""


_TAGS_PROMPT = """You are optimizing YouTube tags for "{channel_name}" to reach the RIGHT viewers.

=== Channel Persona ===
{persona}

=== Video ===
Title: {current_title}
{song_count} tracks:
{songs}

=== Benchmark Analysis (use as viewer-context, may be in Japanese) ===
{benchmark_section}

=== Your Task ===
Generate 15-20 English tags based on VIEWER SEARCH BEHAVIOR.
Prioritize the English keywords/tag_suggestions from the benchmark above (those are validated search terms). Tags themselves are always English.

Think about what viewers actually type:
- Mood searches: "relaxing music", "chill beats to study to", "calm piano"
- Situation searches: "music for studying", "work from home bgm", "sleep music"
- Scene searches: "cafe music", "rainy day music", "late night lounge"
- Genre: "lofi", "jazz", "ambient", "instrumental"
- Duration: "long playlist", "1 hour bgm", "all night music"

Mix broad (high volume) + specific (low competition):
- Broad: "study music", "relaxing bgm", "chill music"
- Specific: "midnight jazz lounge", "golden hour piano", "rainy cafe instrumental"

Respond with a SINGLE JSON object:
{{"tags": ["tag1", "tag2", ...]}}"""


_CONCEPT_PROMPT = """あなたは BGM チャンネル "{channel_name}" のシリーズ編集者です。
1 本の動画を象徴する「端的な日本語コンセプト」を 1 つだけ作ってください。

=== チャンネルペルソナ ===
{persona}

=== この動画 ===
- タイトル: {current_title}
- 公開日: {publish_date}
- 楽曲数: {song_count}
- 楽曲名サンプル:
{songs}

=== ベンチマーク分析（視聴者文脈・日本語混在）===
{benchmark_section}

=== 出力ルール ===
- 必ず日本語の **1 行**、**12〜22 文字** に収める。
- 既存タイトルに引きずられず、ベンチマークの viewer_needs / 雰囲気 / 時間帯 / 場面を踏まえて、その動画が「どんな瞬間に寄り添う BGM か」を視聴者目線で凝縮する。
- 同じチャンネル内の他動画と被らないよう、シーン or 時間帯 or 感情のいずれかを必ず差別化する。
- 句点・引用符は付けない。体言止め可。装飾記号も付けない。

良い例:
- 雨の夜のキッチンで深呼吸する時間
- 終電後の街で灯る一杯の灯り
- 朝霧のオフィスに射し込む金色

JSON で 1 つだけ返してください:
{{"concept": "ここに日本語1行"}}"""


# ─── マスタープロンプト読込（無ければハードコードを使用） ─────
# 探索順:
#   1. アクティブチャンネルフォルダ/.app_channel_config.json["master_prompts"][key]
#   2. ~/.config/{app_id}/master_prompts.json[key]
#   3. ハードコードのフォールバック
# (1) によりチャンネル別プロンプトが Google Drive 経由で 2 PC 間自動同期される。

def _resolve_config_dir_for_proposer() -> Path:
    """app.py と同じロジックで設定ディレクトリを解決。app_id を尊重。"""
    legacy = Path.home() / ".config" / "orzz"
    try:
        if str(Path(__file__).parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).parent))
        from _app_config import resolve_config_dir as _r
        d = _r()
        return d
    except Exception:
        return legacy

_CONFIG_DIR_PROPOSER = _resolve_config_dir_for_proposer()
_MASTER_PROMPTS_FILE = _CONFIG_DIR_PROPOSER / "master_prompts.json"
_DASHBOARD_FILE_PROPOSER = _CONFIG_DIR_PROPOSER / "dashboard_config.json"
_BENCHMARK_CACHE_FILE = _CONFIG_DIR_PROPOSER / "competitor_analysis_cache.json"
_CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"


def _load_benchmark_analysis() -> Optional[dict]:
    """ベンチマーク分析キャッシュを返す（あれば）。analysis 部分のみ。"""
    try:
        if not _BENCHMARK_CACHE_FILE.exists():
            return None
        cache = json.loads(_BENCHMARK_CACHE_FILE.read_text(encoding="utf-8"))
        analysis = cache.get("analysis") if isinstance(cache, dict) else None
        return analysis if isinstance(analysis, dict) else None
    except Exception:
        return None


def _format_thumbnail_section(thumbnail: Optional[Path]) -> str:
    """propose_* プロンプトに埋め込むサムネ Vision 指示ブロック。
    画像があれば Read ツールで読み取らせ、無ければ no-op の placeholder を返す。"""
    if not thumbnail:
        return "\n(no thumbnail found — generate from songs / persona / benchmark only)"
    return (
        f"\nFirst, use the Read tool on this image: '{thumbnail}'\n"
        "After reading the thumbnail, identify the visual scene "
        "(time of day, color palette, subject, mood, setting, dominant emotion). "
        "Anchor your output to what the viewer SEES on the thumbnail — "
        "the title/description should match the visual promise so click + retention align."
    )


def _format_benchmark_section(analysis: Optional[dict], for_japanese_output: bool = False) -> str:
    """propose_* プロンプトに埋め込むベンチマーク文脈ブロックを生成。
    日本語フィールドはそのまま、英語フィールドは英語のままで両言語が混ざる形になる。

    for_japanese_output=True（コンセプト軸など日本語1行出力の経路）では、投稿文軸の
    英語スキャフォールド（description_template / opening_hook / cta_block / hashtag_set）を
    注入しない（日本語出力への英語混入を防ぐ）。"""
    if not analysis:
        return "(no benchmark analysis available — proceed using persona only)"
    bp = analysis.get("buzz_patterns") or {}
    ts = analysis.get("trend_shift") or {}
    rec = analysis.get("recommendations") or {}
    lines = []
    if bp.get("viewer_needs"):
        lines.append("Viewer needs (proven, JP): " + json.dumps(bp.get("viewer_needs"), ensure_ascii=False))
    if bp.get("title_patterns"):
        lines.append("Buzz title patterns (JP): " + json.dumps(bp.get("title_patterns"), ensure_ascii=False))
    if bp.get("keywords"):
        lines.append("Search keywords (EN seeds): " + json.dumps(bp.get("keywords"), ensure_ascii=False))
    if ts.get("from_buzz_to_recent"):
        lines.append("Trend shift (JP): " + str(ts.get("from_buzz_to_recent")))
    if ts.get("underserved_niches"):
        lines.append("Underserved niches (JP): " + json.dumps(ts.get("underserved_niches"), ensure_ascii=False))
    if rec.get("title_tips"):
        lines.append("Title tips (JP): " + json.dumps(rec.get("title_tips"), ensure_ascii=False))
    if rec.get("description_tips"):
        lines.append("Description tips (JP): " + json.dumps(rec.get("description_tips"), ensure_ascii=False))
    if rec.get("tag_suggestions"):
        lines.append("Tag seeds (EN): " + json.dumps(rec.get("tag_suggestions"), ensure_ascii=False))
    # 投稿文軸スキャフォールド（benchmark/description.json）を注入（あれば）。
    # 指定チャンネルの説明文構成から導いた英語テンプレ／フック／CTA／ハッシュタグ。
    # 日本語出力経路（for_japanese_output）には英語テンプレを注入しない。
    try:
        import app_benchmark_description as _bdesc
        scaf = {} if for_japanese_output else _bdesc.get_description_scaffolds()
    except Exception:
        scaf = {}
    if scaf:
        if scaf.get("opening_hook"):
            lines.append("Description opening hook (EN, from trending channels): " + str(scaf.get("opening_hook")))
        if scaf.get("cta_block"):
            lines.append("Description CTA block (EN): " + str(scaf.get("cta_block")))
        if scaf.get("hashtag_set"):
            lines.append("Hashtag set (EN): " + json.dumps(scaf.get("hashtag_set"), ensure_ascii=False))
        if scaf.get("description_template"):
            lines.append("Proven description template (EN, adapt — do NOT copy verbatim): " + str(scaf.get("description_template")))
        if scaf.get("tone_one_line"):
            lines.append("Description tone (JP): " + str(scaf.get("tone_one_line")))
    return "\n".join(lines) if lines else "(benchmark cache present but empty)"


def _channel_master_prompts() -> dict:
    """アクティブチャンネルの master_prompts を返す。

    P2-2: APP_CHANNEL_FOLDER env が立っていれば、global dashboard_config を無視して
    そちらを優先する（複数チャンネル並列ジョブで取り違えを防ぐ）。"""
    try:
        # 優先 1: env override（pipeline / job 経由で渡る）
        import os
        env_folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
        if env_folder:
            p = Path(env_folder) / _CHANNEL_CONFIG_FILENAME
            if p.exists():
                cc = json.loads(p.read_text(encoding="utf-8"))
                mp = cc.get("master_prompts") if isinstance(cc, dict) else None
                return mp if isinstance(mp, dict) else {}
            return {}
        # 優先 2: global dashboard_config（UI active channel）
        if not _DASHBOARD_FILE_PROPOSER.exists():
            return {}
        dc = json.loads(_DASHBOARD_FILE_PROPOSER.read_text(encoding="utf-8"))
        folder = dc.get("channel_folder")
        if not folder:
            return {}
        p = Path(folder) / _CHANNEL_CONFIG_FILENAME
        if not p.exists():
            return {}
        cc = json.loads(p.read_text(encoding="utf-8"))
        mp = cc.get("master_prompts") if isinstance(cc, dict) else None
        return mp if isinstance(mp, dict) else {}
    except Exception:
        return {}


def _load_master_prompt(key: str, fallback: str) -> str:
    """チャンネル別 → グローバル → ハードコードの順でプロンプトを解決。"""
    # 1. チャンネル別
    try:
        cc = _channel_master_prompts()
        v = (cc.get(key) or "").strip() if isinstance(cc.get(key), str) else ""
        if v:
            return v
    except Exception:
        pass
    # 2. グローバル
    try:
        if _MASTER_PROMPTS_FILE.exists():
            data = json.loads(_MASTER_PROMPTS_FILE.read_text(encoding="utf-8"))
            v = (data.get(key) or "").strip()
            if v:
                return v
    except Exception:
        pass
    # 3. ハードコード
    return fallback


# ─── 公開関数 ──────────────────────────────────────────────

def propose_titles(
    *,
    cli_cmd: str = DEFAULT_CLI,
    persona: str = "",
    current_title: str = "",
    song_count: int = 0,
    songs: list[str] | None = None,
    publish_date: str = "",
    count: int = 5,
    channel_name: str = "orzz.",
    benchmark_analysis: Optional[dict] = None,
    thumbnail: Optional[Path] = None,
    **_extra: Any,
) -> list[str]:
    songs_text = "\n".join(f"- {s}" for s in (songs or [])[:40]) or "(none)"
    benchmark_section = _format_benchmark_section(
        benchmark_analysis if benchmark_analysis is not None else _load_benchmark_analysis()
    )
    thumbnail_section = _format_thumbnail_section(thumbnail)
    prompt = _load_master_prompt("title_generation", _TITLES_PROMPT).format(
        channel_name=channel_name or "orzz.",
        persona=persona or "(not set)",
        song_count=song_count,
        songs=songs_text,
        publish_date=publish_date or "(unknown)",
        current_title=current_title or "(none)",
        count=count,
        benchmark_section=benchmark_section,
        thumbnail_section=thumbnail_section,
    )
    # サムネがあれば Read ツールを許可してタイムアウトを延長
    raw = _run_claude(cli_cmd, prompt,
                      timeout=240 if thumbnail else DEFAULT_TIMEOUT,
                      allow_read=bool(thumbnail),
                      read_paths=[thumbnail] if thumbnail else None)
    obj = _extract_json_object(raw)
    if not obj or "titles" not in obj:
        raise RuntimeError(f"JSON 抽出失敗: {raw[:200]}")
    titles = [str(t).strip() for t in obj.get("titles", []) if str(t).strip()]
    return titles[:count]


def propose_description(
    *,
    cli_cmd: str = DEFAULT_CLI,
    persona: str = "",
    current_title: str = "",
    song_count: int = 0,
    songs: list[str] | None = None,
    publish_date: str = "",
    reference: str = "",
    channel_name: str = "orzz.",
    benchmark_analysis: Optional[dict] = None,
    thumbnail: Optional[Path] = None,
    tracklist_text: str = "",
    **_extra: Any,
) -> str:
    songs_text = "\n".join(f"- {s}" for s in (songs or [])[:40]) or "(none)"
    benchmark_section = _format_benchmark_section(
        benchmark_analysis if benchmark_analysis is not None else _load_benchmark_analysis()
    )
    thumbnail_section = _format_thumbnail_section(thumbnail)
    prompt = _load_master_prompt("description_generation", _DESCRIPTION_PROMPT).format(
        channel_name=channel_name or "orzz.",
        persona=persona or "(not set)",
        current_title=current_title or "(none)",
        song_count=song_count,
        songs=songs_text,
        publish_date=publish_date or "(unknown)",
        reference=reference[:2000] if reference else "(no reference)",
        benchmark_section=benchmark_section,
        thumbnail_section=thumbnail_section,
    )
    raw = _run_claude(cli_cmd, prompt,
                      timeout=300 if thumbnail else DEFAULT_TIMEOUT,
                      allow_read=bool(thumbnail),
                      read_paths=[thumbnail] if thumbnail else None)
    obj = _extract_json_object(raw)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {raw[:200]}")
    if "opening" in obj or "closing" in obj:
        opening = str(obj.get("opening", "")).strip()
        closing = str(obj.get("closing", "")).strip()
    elif "description" in obj:
        # 旧フォーマット互換（マスタープロンプト未更新のユーザー向け）
        full = str(obj["description"]).strip()
        return _inject_tracklist_into_legacy(full, tracklist_text)
    else:
        raise RuntimeError(f"JSON に opening/closing/description が無い: {raw[:200]}")
    return _compose_description(opening, tracklist_text, closing)


def _compose_description(opening: str, tracklist_text: str, closing: str) -> str:
    """OPENING / Tracklist / CLOSING を 1 本に結合。"""
    parts: list[str] = []
    if opening:
        parts.append(opening)
    if tracklist_text:
        parts.append("Tracklist\n" + tracklist_text.strip())
    if closing:
        parts.append(closing)
    return "\n\n".join(parts).strip()


def _inject_tracklist_into_legacy(description: str, tracklist_text: str) -> str:
    """旧プロンプト戻り値（description 一体型）に対し、Tracklist を強制差し替え。

    LLM が書いた Tracklist 風セクションは捨て、正規 tracklist_text を挿入する。
    検出ヒントが無ければ末尾に追加。"""
    if not tracklist_text:
        return description
    lines = description.splitlines()
    header_re = re.compile(r"^\s*(?:[—\-–=]+\s*)?(?:track\s*list|tracklist|収録曲|【\s*tracklist)", re.IGNORECASE)
    tc_re = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\b")
    start = None
    for i, ln in enumerate(lines):
        if header_re.match(ln) or tc_re.match(ln):
            start = i
            break
    if start is None:
        return (description.rstrip() + "\n\n" + "Tracklist\n" + tracklist_text.strip()).strip()
    # Tracklist 領域の終端を探す（連続するタイムコード行 + 空行まで）
    end = start
    while end < len(lines):
        ln = lines[end]
        if not ln.strip():
            # 空行が来てもタイムコード行が続くなら継続
            j = end + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and tc_re.match(lines[j]):
                end = j
                continue
            break
        end += 1
    before = "\n".join(lines[:start]).rstrip()
    after = "\n".join(lines[end:]).lstrip()
    block = "Tracklist\n" + tracklist_text.strip()
    return "\n\n".join(p for p in [before, block, after] if p).strip()


def propose_tags(
    *,
    cli_cmd: str = DEFAULT_CLI,
    persona: str = "",
    current_title: str = "",
    song_count: int = 0,
    songs: list[str] | None = None,
    channel_name: str = "orzz.",
    benchmark_analysis: Optional[dict] = None,
    **_extra: Any,  # gather_context() の publish_date / thumbnail を吸収
) -> list[str]:
    songs_text = "\n".join(f"- {s}" for s in (songs or [])[:40]) or "(none)"
    benchmark_section = _format_benchmark_section(
        benchmark_analysis if benchmark_analysis is not None else _load_benchmark_analysis()
    )
    prompt = _load_master_prompt("tags_generation", _TAGS_PROMPT).format(
        channel_name=channel_name or "orzz.",
        persona=persona or "(not set)",
        current_title=current_title or "(none)",
        song_count=song_count,
        songs=songs_text,
        benchmark_section=benchmark_section,
    )
    raw = _run_claude(cli_cmd, prompt)
    obj = _extract_json_object(raw)
    if not obj or "tags" not in obj:
        raise RuntimeError(f"JSON 抽出失敗: {raw[:200]}")
    tags = [str(t).strip() for t in obj.get("tags", []) if str(t).strip()]
    return tags[:30]


def propose_concept(
    *,
    cli_cmd: str = DEFAULT_CLI,
    persona: str = "",
    current_title: str = "",
    song_count: int = 0,
    songs: list[str] | None = None,
    publish_date: str = "",
    channel_name: str = "orzz.",
    benchmark_analysis: Optional[dict] = None,
    **_extra: Any,  # gather_context() の thumbnail などを吸収
) -> str:
    """1 動画の「端的な日本語コンセプト」を 1 行返す。
    ベンチマーク分析を主軸に、視聴者目線で 12〜22 字に凝縮する。"""
    songs_text = "\n".join(f"- {s}" for s in (songs or [])[:20]) or "(none)"
    benchmark_section = _format_benchmark_section(
        benchmark_analysis if benchmark_analysis is not None else _load_benchmark_analysis(),
        for_japanese_output=True,
    )
    prompt = _load_master_prompt("concept_generation", _CONCEPT_PROMPT).format(
        channel_name=channel_name or "orzz.",
        persona=persona or "(not set)",
        current_title=current_title or "(none)",
        song_count=song_count,
        songs=songs_text,
        publish_date=publish_date or "(unknown)",
        benchmark_section=benchmark_section,
    )
    raw = _run_claude(cli_cmd, prompt, timeout=120)
    obj = _extract_json_object(raw)
    if not obj or "concept" not in obj:
        raise RuntimeError(f"JSON 抽出失敗: {raw[:200]}")
    concept = str(obj["concept"]).strip().strip('"').strip("「」")
    # 改行 → 空白、過剰な装飾を除去、長さ上限
    concept = re.sub(r"\s+", " ", concept).strip("。．.!?！？")
    if len(concept) > 40:
        concept = concept[:40]
    return concept


# ─── フォルダからコンテキスト収集 ───

def gather_context(folder: Path) -> dict[str, Any]:
    """動画フォルダから Claude に渡すコンテキストを抽出"""
    folder = Path(folder)
    out: dict[str, Any] = {
        "current_title": "",
        "song_count": 0,
        "songs": [],
        "publish_date": "",
        "thumbnail": None,
        "tracklist_text": "",
    }
    # タイトル
    tf = folder / "youtube_title.txt"
    if tf.exists():
        out["current_title"] = tf.read_text(encoding="utf-8").strip()
    # 楽曲名
    music_dir = folder / "music"
    if music_dir.is_dir():
        songs = [p.stem for p in sorted(music_dir.glob("*.mp3"))]
        out["songs"] = songs
        out["song_count"] = len(songs)
    # 公開日
    m = re.search(r"_(\d{6})$", folder.name)
    if m:
        ds = m.group(1)
        try:
            out["publish_date"] = f"20{ds[:2]}-{ds[2:4]}-{ds[4:6]}"
        except Exception:
            pass
    # サムネイル（Vision 入力用、無ければ None）
    out["thumbnail"] = _find_thumbnail(folder)
    # 正規タイムコード（LOOP 直前まで）— description の Tracklist 差し込み用
    out["tracklist_text"] = _read_tracklist_until_loop(folder)
    return out


def _read_tracklist_until_loop(folder: Path) -> str:
    """music_time_code_info_*.txt を読み、LOOP 行の直前まで返す。
    folder.name 先頭の vol 番号を優先し、見つからなければ任意の一致を採用。"""
    folder = Path(folder)
    m = re.match(r"^(\d+)_", folder.name)
    vol = m.group(1) if m else ""
    candidates: list[Path] = []
    if vol:
        candidates.append(folder / f"music_time_code_info_{vol}.txt")
        try:
            candidates.append(folder / f"music_time_code_info_{int(vol)}.txt")
            candidates.append(folder / f"music_time_code_info_{int(vol):02d}.txt")
        except Exception:
            pass
    candidates.extend(sorted(folder.glob("music_time_code_info_*.txt")))
    seen: set[Path] = set()
    for p in candidates:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        out_lines: list[str] = []
        for line in lines:
            if "LOOP" in line.upper():
                break
            out_lines.append(line.rstrip())
        return "\n".join(out_lines).strip()
    return ""
