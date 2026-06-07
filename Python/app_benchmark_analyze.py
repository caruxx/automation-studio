#!/usr/bin/env python3
"""ベンチマーク分析・提案層（D6: ベンチ二系統正規化／実体物理移動済）。

competitor（取得層）から物理的に分離した、競合分析を起点とする分析・提案関数の本体:
  - analyze_with_claude        競合データ → バズ/トレンド/方向性の総合分析
  - propose_with_analysis      分析 → タイトル/説明/タグのメタ提案
  - propose_suno_prompt        分析 → SUNO プロンプト提案
  - analyze_thumbnail_elements サムネ画像 → 視覚要素抽出(Vision)

下流（app.py / app_pipeline）はこのモジュールを単一窓口として参照する。
本モジュールは app_competitor を import しない（取得層への逆依存なし＝循環なし）。
competitor 側の内部利用（run_full_analysis / __main__）は本モジュールから局所 import する。
LLM 呼び出し（run_llm / run_llm_vision）と app_benchmark_description は各関数内で局所 import。
"""
from __future__ import annotations

import json
from pathlib import Path

from app_benchmark_common import extract_json_object as _extract_json_object

DEFAULT_CLI = "claude"


def analyze_with_claude(competitor_data: dict, cli_cmd: str = DEFAULT_CLI, growth_summary: dict = None) -> dict:
    """競合データを Claude CLI で分析"""
    # データを要約（プロンプトが長すぎないように）
    summary_lines = []
    for ch in competitor_data.get("channels", []):
        summary_lines.append(f"\n=== {ch['channelName']} ({ch['totalVideos']} videos) ===")
        summary_lines.append("\n[TOP 10 by views]")
        for v in ch["topByViews"]:
            summary_lines.append(
                f"  {v['viewCount']:>10,} views | {v['title']}"
            )
        summary_lines.append("\n[Recent 10 uploads]")
        for v in ch["recentUploads"]:
            summary_lines.append(
                f"  {v['viewCount']:>10,} views | {v['publishedAt'][:10]} | {v['title']}"
            )
    summary = "\n".join(summary_lines)

    # 成長データがあれば注入
    growth_section = ""
    if growth_summary and growth_summary.get("hot_channels"):
        growth_lines = ["\n=== Growth Signals (auto-tracked daily) ===",
                        "Hot channels by composite score (ACTIVELY GROWING — weight their strategies heavily):"]
        for i, h in enumerate(growth_summary["hot_channels"][:10], 1):
            growth_lines.append(
                f"  {i}. {h['name']}: +{h['daily_views']:,} views/day, "
                f"+{h['daily_subs']} subs/day, {h['growth_rate']}% growth, "
                f"total {h['total_views']:,} views, {h['subscribers']:,} subs"
            )
        growth_section = "\n".join(growth_lines)

    prompt = f"""## 出力言語ルール（最優先）
このベンチマーク分析は日本語の運用者が読むためのものです。
JSON 内の説明・分析・提案・理由はすべて自然な日本語で書いてください。
検索語、ジャンル名、楽器名などで英語表記が必要な場合だけ、短い英語語句を日本語説明の中に含めても構いません。
英語だけの文章、英語だけの提案、英語だけの理由は禁止です。

[数値 — 数字のまま]
- buzz_patterns.avg_title_length
- music_direction.bpm_range.min / max

---

あなたは YouTube の BGM/インストゥルメンタル音楽チャンネルを視聴者心理の観点で分析するエキスパートです。

{summary}
{growth_section}

=== 分析フレームワーク ===
視聴者の立場で考えてください:
- なぜ視聴者は再生回数上位の動画をクリックしたのか？どんな感情ニーズに応える約束だったか？
- 人気タイトルが想起させるシーン・感情は何か？
- 視聴者はこの種のコンテンツをどんなキーワードで検索するか？（study music, sleep music, cafe bgm 等）
- 視聴者の欲求と直近アップロードのギャップはどこか？

次の単一 JSON オブジェクトで回答してください（言語ルール厳守）:
{{
  "buzz_patterns": {{
    "title_patterns": ["例: 深夜の作業に寄り添う静かなジャズピアノ", "例: 雨の暖炉カフェで深い集中"],
    "keywords": ["例: study music（勉強用BGM）", "例: late night focus（深夜作業の集中）"],
    "viewer_needs": ["例: 仕事後の緊張を緩めながら集中の余韻を残したい", "例: 一人の作業時間に誰かが寄り添ってくれる感覚が欲しい"],
    "avg_title_length": 50,
    "common_structures": ["例: 場所 + 時間帯 + 解決したい感情ジョブ"]
  }},
  "trend_shift": {{
    "from_buzz_to_recent": "例: 過去のヒットは『静けさで集中させる』が中心だったが、最近の投稿は朝のリセット時間という柔らかい入口を試している",
    "emerging_needs": ["例: タスクの合間の 30 分リセット", "例: 在宅勤務の切り替え時の暖かい BGM"],
    "underserved_niches": ["例: 90 分連続のディープフォーカス", "例: 深夜まで残業する人のためのリビング BGM"]
  }},
  "recommendations": {{
    "title_tips": ["例: ジャンル名より先に具体的な瞬間や場所を置く"],
    "description_tips": ["例: 楽曲説明より先に視聴者の内面状態に触れる"],
    "tag_suggestions": ["例: study music（勉強用BGM）", "例: focus music（集中BGM）"]
  }},
  "music_direction": {{
    "recommended_genres": ["例: ローファイ・ヒップホップ", "例: ジャズピアノ", "例: アンビエント"],
    "bpm_range": {{"min": 60, "max": 80}},
    "mood_tags": ["例: 温かい", "例: もの寂しい", "例: ノスタルジック"],
    "instrumentation": ["例: ローズピアノ", "例: 柔らかいドラム", "例: テープヒス"],
    "reference_vibe": "例: ネオンが窓に滲む、雨の深夜の街角カフェ",
    "avoid": ["例: クラブ向けの高BPM感", "例: 明るすぎるソロピアノ"]
  }},
  "visual_direction": {{
    "color_palette": ["例: 深い琥珀色", "例: ネイビー", "例: 暖かいセピア"],
    "time_of_day": "例: 深夜 23:00〜2:00",
    "subjects": ["例: 窓辺で本を読む人", "例: 閉店後もまだ灯りが残るカフェ"],
    "composition": "例: ワイドな構図、浅い被写界深度、ローキーのシネマティックライティング",
    "atmosphere": "例: 静謐、内省的、シネマティック",
    "avoid": ["例: 彩度が強すぎる色", "例: カメラ目線の強い人物"]
  }}
}}

ルール:
- すべての洞察は『クリエイター視点の好み』ではなく『視聴者ニーズ』に紐づけること
- クリックを駆動する感情トリガー（安らぎ・逃避・集中・郷愁）を特定する
- music_direction / visual_direction は、上位動画のタイトル・説明・タグから『そのタイトルの約束を満たすには どんな音 / どんな映像が必要か』をリバースエンジニアリングする
- music bpm_range は BGM として現実的な範囲（通常 50-100）
- 言語ルール（先頭の出力言語ルール）を厳守し、分析文・提案文・理由文は日本語で書く
- JSON オブジェクトのみを出力（前置き・コードフェンス不要）
"""

    print("🧠 Claude CLI で競合分析中...")
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="competitor-analyze")

    obj = _extract_json_object(out)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {out[:300]}")

    print("  ✓ 分析完了")
    return obj


def propose_with_analysis(
    analysis: dict,
    competitor_data: dict,
    cli_cmd: str = DEFAULT_CLI,
    current_title: str = "",
    songs: list[str] | None = None,
    persona: str = "",
    growth_summary: dict | None = None,
) -> dict:
    """競合分析を踏まえたタイトル・説明・タグを提案"""

    # バズ動画のタイトル例を抽出
    buzz_titles = []
    for ch in competitor_data.get("channels", []):
        for v in ch.get("topByViews", [])[:5]:
            buzz_titles.append(f"  {v['viewCount']:,} views: {v['title']}")
    buzz_examples = "\n".join(buzz_titles[:15])

    growth_lines = []
    for h in (growth_summary or {}).get("hot_channels", [])[:10]:
        growth_lines.append(
            f"  {h.get('name','')}: +{int(h.get('daily_views') or 0):,} views/day, "
            f"+{int(h.get('daily_subs') or 0):,} subs/day, "
            f"{h.get('growth_rate', 0)}% growth, score {h.get('score', '')}"
        )
    growth_examples = "\n".join(growth_lines) or "(no ChannelTracker growth data)"

    songs_text = "\n".join(f"- {s}" for s in (songs or [])[:30]) or "(none)"

    bp = analysis.get('buzz_patterns', {})
    ts = analysis.get('trend_shift', {})
    viewer_needs = json.dumps(bp.get('viewer_needs', bp.get('title_patterns', [])), ensure_ascii=False)
    underserved = json.dumps(ts.get('underserved_niches', ts.get('emerging_needs', [])), ensure_ascii=False)

    # 投稿文軸スキャフォールド（benchmark/description.json）を注入（あれば）。
    # 指定チャンネルの説明文構成から導いた英語テンプレ／フック／CTA／ハッシュタグ。
    desc_scaffold_block = ""
    try:
        import app_benchmark_description as _bdesc
        _scaf = _bdesc.get_description_scaffolds()
        if _scaf:
            _parts = []
            if _scaf.get("opening_hook"):
                _parts.append(f"Opening hook style: {_scaf['opening_hook']}")
            if _scaf.get("cta_block"):
                _parts.append(f"CTA block:\n{_scaf['cta_block']}")
            if _scaf.get("hashtag_set"):
                _parts.append("Hashtag set: " + " ".join(_scaf["hashtag_set"]))
            if _scaf.get("description_template"):
                _parts.append("Proven description template (adapt to THIS video, do NOT copy verbatim):\n"
                              + str(_scaf["description_template"]))
            if _scaf.get("tone_one_line"):
                _parts.append(f"Tone note (JP context): {_scaf['tone_one_line']}")
            desc_scaffold_block = "\n".join(_parts)
    except Exception:
        desc_scaffold_block = ""

    prompt = f"""You are a viewer psychology expert and YouTube growth strategist crafting English metadata that deeply resonates with the audience.

NOTE on input language:
- "Channel Persona" and the "What Viewers Need" insights below may be written in Japanese — they are operator-facing notes about viewer psychology.
- Use them as CONTEXT for understanding the viewer, then write all output (titles / description / tags) in natural English only.
- The "Search keywords viewers use" list is already English — use those tokens directly.

=== Channel Persona ===
{persona or 'AI-generated instrumental BGM, lounge, chill, jazz'}

=== This Video ===
Current title: {current_title or '(none)'}
Songs:
{songs_text}

=== What Viewers Need (from competitor analysis) ===
Proven viewer needs (Japanese context): {viewer_needs}
Search keywords viewers use (English seeds): {json.dumps(bp.get('keywords', []), ensure_ascii=False)}
Underserved niches / opportunity (Japanese context): {underserved}
Trend shift (Japanese context): {ts.get('from_buzz_to_recent', '')}

=== Top performing titles (proof of what viewers click) ===
{buzz_examples}

=== ChannelTracker Growth Signals (proof of what is currently moving) ===
{growth_examples}

=== Description Structure (from trending channels' posting style) ===
{desc_scaffold_block or '(no description-axis analysis yet — use the Description Rules below)'}

=== Your Task ===
Create English metadata that makes the viewer feel: "This is exactly what I was looking for."

Respond with a SINGLE JSON object:
{{
  "titles": ["title1", "title2", "title3", "title4", "title5"],
  "description": "full YouTube description",
  "tags": ["tag1", "tag2", ...]
}}

Title Rules:
- English only.
- Each title must address a specific VIEWER MOMENT: studying late, rainy morning commute, winding down after work, Sunday afternoon, can't sleep at 3am
- Paint the scene the viewer wants to step into.
- Use copywriting discipline: a concrete promise, a clear category entry point, one emotional job-to-be-done, and enough specificity to feel made for one person.
- Avoid generic keyword piles. The strongest title should pass the "oh, this is exactly it" test for the channel persona.
- Use sensory/emotional words: warm, soft, golden, midnight, gentle, rain
- Under 60 chars. English. 5 distinct options targeting different viewer situations.

Description Rules:
- English only.
- Open by speaking TO the viewer about their current moment (not about the music)
- "Still awake? Let this carry you to somewhere quiet." — that kind of empathy
- Ground the copy in viewer psychology: need recognition, relief, imagined scene, and a gentle reason to stay.
- Include tracklist with timecodes
- Close with hashtags matching viewer search behavior
- If a "Description Structure" block is provided above, FOLLOW its proven structure (opening hook → tracklist with timecodes → CTA → hashtags) and adapt it to THIS video — never copy competitor text verbatim.

Tag Rules:
- 15-20 tags based on what viewers ACTUALLY SEARCH
- Mix: situation tags (study music, work bgm) + mood tags (chill, relaxing) + scene tags (cafe, rain) + genre tags
- English tags only.

Output ONLY the JSON object.
"""

    print("🎯 Claude CLI で最適化提案中...")
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="propose-with-analysis")

    obj = _extract_json_object(out)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {out[:300]}")

    print("  ✓ 提案完了")
    return obj


def propose_suno_prompt(
    analysis: dict,
    current_title: str = "",
    existing_prompt: str = "",
    cli_cmd: str = DEFAULT_CLI,
) -> dict:
    """competitor 分析の music_direction を SUNO 向けの日本語プロンプト 1 本に変換"""
    md = (analysis or {}).get("music_direction") or {}
    if not md:
        raise RuntimeError("analysis.music_direction が空です。競合分析を再実行してください (analysis_outdated)")

    bp = (analysis or {}).get("buzz_patterns") or {}
    ts = (analysis or {}).get("trend_shift") or {}

    prompt = f"""あなたは YouTube BGM チャンネル向けに、SUNO で使う音楽生成プロンプトを 1 本作る専門家です。
目的は、ベンチマーク先の視聴者が実際に反応している要素を、自チャンネル用のインストゥルメンタルBGMへ翻訳することです。

=== 音楽方向性（ベンチマーク先のヒット傾向から逆算） ===
Recommended genres: {json.dumps(md.get('recommended_genres', []))}
BPM range: {json.dumps(md.get('bpm_range', {}))}
Mood tags: {json.dumps(md.get('mood_tags', []))}
Instrumentation: {json.dumps(md.get('instrumentation', []))}
Reference vibe: {md.get('reference_vibe', '')}
Avoid: {json.dumps(md.get('avoid', []))}

=== 視聴者文脈（リスナーが求めていること） ===
Viewer needs: {json.dumps(bp.get('viewer_needs', []))}
Underserved niches: {json.dumps(ts.get('underserved_niches', []))}

=== 現在の状態 ===
Current video title: {current_title or '(none)'}
Existing prompt (for reference; may be empty): {existing_prompt or '(none)'}

=== タスク ===
SUNO に入れる 1 行プロンプトを日本語で出力してください（目安 120〜220 字）。
- ジャンル + ムード + 主要楽器から始める
- 意味がある場合だけ BPM / テンポ感を入れる（例: ゆったり 70bpm）
- ボーカルなし。インストゥルメンタル BGM のみ
- SUNO、AI、メタ概念には触れず、音楽そのものを描写する
- reference_vibe から具体的な感覚シーンを 1 つ入れる（例: 雨の深夜カフェ）
- 実在するアーティスト名、バンド名、作曲家名、プロデューサー名、レーベル名、実在曲名・アルバム名は絶対に入れない

次の単一 JSON だけを返してください。prompt と rationale はどちらも日本語:
{{
  "prompt": "<SUNO プロンプト。日本語で 1 行>",
  "rationale": "<なぜベンチマーク視聴者ニーズに合うか。日本語で 1〜2 文>"
}}

JSON オブジェクトのみを出力してください。
"""

    def _as_list(value):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _local_suno_prompt(reason: str) -> dict:
        genres = _as_list(md.get("recommended_genres"))[:2]
        moods = _as_list(md.get("mood_tags"))[:4]
        instruments = _as_list(md.get("instrumentation"))[:4]
        avoid = _as_list(md.get("avoid"))[:3]
        vibe = str(md.get("reference_vibe") or "").strip()
        bpm = md.get("bpm_range") or {}
        tempo = ""
        if isinstance(bpm, dict):
            lo = bpm.get("min") or bpm.get("low")
            hi = bpm.get("max") or bpm.get("high")
            if lo and hi:
                tempo = f"{lo}-{hi}bpm"
            elif lo or hi:
                tempo = f"{lo or hi}bpm前後"

        lead = "、".join(genres) if genres else "洗練されたインストゥルメンタルBGM"
        mood_text = "、".join(moods) if moods else "集中できる落ち着いたムード"
        inst_text = "、".join(instruments) if instruments else "柔らかなシンセ、控えめなピアノ、深い低音"
        scene = vibe or (current_title or "夜のワークスペースで静かに没入する空気感")
        parts = [
            f"{lead}。{mood_text}。",
            f"主要楽器は{inst_text}。",
        ]
        if tempo:
            parts.append(f"テンポは{tempo}で自然に前へ進む。")
        parts.append(f"{scene}を感じる、ボーカルなしの作業用BGM。")
        if avoid:
            parts.append(f"{'、'.join(avoid)}は避ける。")
        local_prompt = "".join(parts)
        return {
            "prompt": local_prompt[:260],
            "rationale": f"Claude CLIが使えないため、競合分析のmusic_directionからローカル生成しました。{reason[:120]}",
            "fallback": "local",
        }

    print("🎵 Claude CLI で SUNO プロンプト提案中...")
    # Claude → Codex（共通ランナー）→ ローカル生成 の3段フォールバック
    from app_llm_runner import run_llm, LLMError
    try:
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="suno-prompt")
    except LLMError as e:
        print(f"  ⚠ Claude/Codex 失敗。ローカル生成にフォールバック: {e}")
        return _local_suno_prompt(str(e))

    obj = _extract_json_object(out)
    if not obj or not obj.get("prompt"):
        reason = f"JSON 抽出失敗: {out[:300]}"
        print(f"  ⚠ {reason}。ローカル生成にフォールバック")
        return _local_suno_prompt(reason)

    print("  ✓ SUNO プロンプト提案完了")
    return obj


# propose_flow_prompt は D8 で削除（Flow 撤去）。Flow プロンプト生成は不要。

def analyze_thumbnail_elements(
    image_paths: list,
    url_list: list = None,
    context_hint: str = "",
    cli_cmd: str = DEFAULT_CLI,
) -> dict:
    """競合サムネ画像から要素抽出 → 自チャンネルへの落とし込み方針を返す。

    **重要な設計方針**:
    - 同じ画像を生成することが目的ではない
    - 主要な視覚要素・視聴者に受けているポイントを抽出する
    - 抽出した要素を orzz. チャンネルの世界観に"翻訳"する方針を示す

    Args:
        image_paths: ローカル画像ファイルのパス一覧（複数可）
        url_list: リモート画像URL一覧（あれば /tmp に DL してから解析）
        context_hint: ユーザーからの補足（自チャンネルの方向性、避けたい要素など）
        cli_cmd: Claude CLI パス

    Returns:
        {
          "element_extraction": {...},  # 構図/配色/被写体/ライティング等の抽出結果
          "viewer_hooks": [...],         # 視聴者に受けているポイント
          "adaptation_hints": {...},     # orzz. チャンネル向けに翻訳する際の方針
          "avoid": [...]                 # コピーを避けるべき具体要素（ブランド毀損防止）
        }
    """
    import tempfile
    import urllib.request

    local_paths = [Path(p) for p in (image_paths or []) if Path(p).exists()]
    tmp_dir = None
    downloaded = []
    if url_list:
        tmp_dir = Path(tempfile.mkdtemp(prefix="orzz_thumb_"))
        for i, url in enumerate(url_list):
            try:
                ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
                if ext not in {"png", "jpg", "jpeg", "webp"}:
                    ext = "jpg"
                dst = tmp_dir / f"thumb_{i:02d}.{ext}"
                urllib.request.urlretrieve(url, dst)
                downloaded.append(dst)
            except Exception as e:
                print(f"  ⚠ URL 取得失敗: {url} ({e})")
        local_paths.extend(downloaded)

    if not local_paths:
        raise RuntimeError("解析対象の画像がありません。ファイル or URL を1つ以上指定してください")

    # Claude CLI が Read ツールでパスを読めるようにパスをそのまま埋め込む
    image_refs = "\n".join(f"  - {p}" for p in local_paths)

    prompt = f"""あなたは YouTube サムネイル分析のエキスパートです。以下の競合サムネ画像を解析し、
**視聴者に受けているポイント**と**主要な視覚要素**を抽出してください。

## 重要な制約（必ず守ること）
1. **コピー・模倣・再現を目的としない**。同じ画像を作ることが目的ではない。
2. 要素を抽出した上で、**orzz. BGM チャンネルの世界観に翻訳する方針**を示す。
3. ブランド毀損リスクのある要素（チャンネルロゴ・特徴的な人物・著作権物）は "avoid" に列挙する。

## 分析対象の画像
{image_refs}

## ユーザーからの補足
{context_hint or '(なし)'}

## 出力形式（必ず JSON 1 オブジェクトで返す）
```json
{{
  "element_extraction": {{
    "composition": "画角・被写体配置・視線誘導",
    "color_palette": ["支配色1", "支配色2", ...],
    "lighting": "時間帯・光源・陰影",
    "subjects": ["主要被写体1", "主要被写体2", ...],
    "atmosphere": "情緒・温度感・雰囲気",
    "text_overlay": "文字要素の特徴（あれば）"
  }},
  "viewer_hooks": [
    "視聴者がクリックしたくなる心理的フック1（例: 郷愁・安心・没入感）",
    "視聴者がクリックしたくなる心理的フック2"
  ],
  "adaptation_hints": {{
    "keep": ["orzz. に取り入れるべき抽象要素（色温度・構図パターン等）"],
    "transform": ["そのまま使わず orzz. 流に翻訳するべき要素（具体的被写体→抽象化等）"],
    "orzz_vibe": "orzz. チャンネルでこの方向性を表現する際の1文サマリー",
    "gpt_image2_prompt_seed": "5要素（被写体/背景/ライティング/スタイル/カメラ構図）へ落とすための英語1文"
  }},
  "avoid": [
    "模倣・コピーしてはいけない具体要素（著作権・特徴的ブランド要素等）"
  ]
}}
```

JSON オブジェクトのみを出力してください。余計な説明文・マークダウンは不要。
"""

    print(f"🖼 Claude Vision で {len(local_paths)} 枚のサムネ要素分析中...")
    # Vision 共通ランナーに委譲（Claude→Codex フォールバック・画像は -i / --add-dir で渡す）
    from app_llm_runner import run_llm_vision, LLMError
    try:
        out = run_llm_vision(prompt, local_paths, cli_cmd=cli_cmd, timeout=600, label="thumb-elements")
    finally:
        # tmpクリーンアップ（成否に関わらず）
        if tmp_dir:
            try:
                for f in downloaded:
                    f.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except Exception:
                pass

    obj = _extract_json_object(out)
    if not obj or not obj.get("element_extraction"):
        raise RuntimeError(f"JSON 抽出失敗: {out[:300]}")

    print(f"  ✓ サムネ要素抽出完了（{len(local_paths)} 枚）")
    return obj


__all__ = [
    "analyze_with_claude",
    "propose_with_analysis",
    "propose_suno_prompt",
    "analyze_thumbnail_elements",
]
