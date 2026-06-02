#!/usr/bin/env python3
"""orzz. 競合チャンネル分析 + AI タイトル/説明文 最適化提案
==========================================================

ライバルチャンネルの「バズ動画 TOP10」「直近投稿 10本」を YouTube Data API で取得し、
Claude CLI に分析させて、自チャンネル動画のタイトル・説明・タグを最適化提案する。

使い方:
  # 分析のみ（JSON 出力）
  python3 app_competitor.py --analyze

  # 分析 + vol.78 向けに提案
  python3 app_competitor.py --propose 78
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 設定ディレクトリ（v2 配布化対応・共通モジュール経由）
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
CLIENT_SECRET = CONFIG_DIR / "youtube_client_secret.json"
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"
CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
BENCHMARK_CONFIG_FILE = CONFIG_DIR / "benchmark_config.json"
_CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def _check_token_scopes():
    """既存トークンのスコープを確認。readonly が無ければ再認証が必要"""
    if not TOKEN_FILE.exists():
        return False, "トークン未作成"
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        token_scopes = set(data.get("scopes", []))
        required = {"https://www.googleapis.com/auth/youtube.readonly"}
        if not required.issubset(token_scopes):
            return False, f"youtube.readonly スコープ不足。現スコープ: {token_scopes}"
        return True, "OK"
    except Exception as e:
        return False, str(e)

DEFAULT_CLI = "claude"

try:
    from app_image_prompt import build_gpt_image2_prompt, normalize_visual_direction
except Exception:  # pragma: no cover - keep legacy CLI scripts importable
    build_gpt_image2_prompt = None
    normalize_visual_direction = None


def _load_json_dict(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_channel_config_for_analysis(dashboard_cfg: dict) -> dict:
    folder = (dashboard_cfg.get("channel_folder") or "").strip()
    if not folder:
        return {}
    return _load_json_dict(Path(folder) / _CHANNEL_CONFIG_FILENAME)


def _load_analysis_config() -> dict:
    """分析実行用に dashboard / benchmark / active channel 設定を合成する。

    app.py 側はベンチマーク設定を benchmark_config.json に分離し、
    チャンネル依存値を <channel_folder>/.app_channel_config.json に保存する。
    CLI スクリプトも同じ実効設定を読む必要がある。
    """
    dashboard = _load_json_dict(CONFIG_DIR / "dashboard_config.json")
    benchmark = _load_json_dict(BENCHMARK_CONFIG_FILE)
    channel_cfg = _load_channel_config_for_analysis(dashboard)

    cfg = dict(dashboard)
    cfg.update({
        "spreadsheet_channel_detail_url": benchmark.get("spreadsheet_channel_detail_url")
        or dashboard.get("spreadsheet_channel_detail_url")
        or channel_cfg.get("spreadsheet_channel_detail_url", ""),
        "spreadsheet_growth_tracking_url": benchmark.get("spreadsheet_growth_tracking_url")
        or dashboard.get("spreadsheet_growth_tracking_url")
        or channel_cfg.get("spreadsheet_growth_tracking_url", ""),
        "benchmark_filter": benchmark.get("filter")
        or dashboard.get("benchmark_filter")
        or channel_cfg.get("benchmark_filter")
        or {},
        "benchmark_pinned_names": benchmark.get("pinned_names")
        or dashboard.get("benchmark_pinned_names")
        or channel_cfg.get("benchmark_pinned_names")
        or [],
        "rival_channels": channel_cfg.get("rival_channels")
        or dashboard.get("rival_channels")
        or [],
    })
    return cfg


# ─── YouTube API ───────────────────────────────────────

def _get_youtube_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    need_reauth = False

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        # スコープ不足チェック
        token_scopes = set(creds.scopes or [])
        required = {"https://www.googleapis.com/auth/youtube.readonly"}
        if not required.issubset(token_scopes):
            print("  ⚠️ youtube.readonly スコープ不足 → 再認証")
            need_reauth = True
            creds = None

    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json())
                print("  ✓ トークンリフレッシュ成功")
            except Exception as e:
                print(f"  ⚠️ リフレッシュ失敗: {e} → 再認証")
                need_reauth = True
                creds = None

    if not creds or need_reauth:
        if not CLIENT_SECRET.exists():
            raise RuntimeError(f"OAuth ファイル未配置: {CLIENT_SECRET}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=8080)
        TOKEN_FILE.write_text(creds.to_json())
        print("  ✓ 新規認証完了")

    return build("youtube", "v3", credentials=creds)


def _extract_channel_id(youtube, url: str) -> str | None:
    """チャンネル URL から channelId を解決"""
    # /channel/UC... 形式
    m = re.search(r"/channel/(UC[\w-]+)", url)
    if m:
        return m.group(1)
    # /@handle 形式
    m = re.search(r"/@([\w.-]+)", url)
    if m:
        handle = m.group(1)
        r = youtube.search().list(q=f"@{handle}", type="channel", part="id", maxResults=1).execute()
        items = r.get("items", [])
        if items:
            return items[0]["id"]["channelId"]
    # /c/name or /user/name
    m = re.search(r"/(c|user)/([\w.-]+)", url)
    if m:
        r = youtube.search().list(q=m.group(2), type="channel", part="id", maxResults=1).execute()
        items = r.get("items", [])
        if items:
            return items[0]["id"]["channelId"]
    # watch?v=VIDEO_ID → 動画からチャンネルを逆引き
    m = re.search(r"[?&]v=([\w-]{11})", url)
    if m:
        vid = m.group(1)
        r = youtube.videos().list(part="snippet", id=vid).execute()
        items = r.get("items", [])
        if items:
            return items[0]["snippet"]["channelId"]
    return None


def _fetch_channel_videos(youtube, channel_id: str, max_results: int = 50, desc_limit: int = 500):
    """チャンネルの動画一覧を取得（snippet + statistics）。

    desc_limit: 説明文の切り詰め文字数（既定500=従来挙動）。投稿文軸の構成分析では
    末尾のCTA/ハッシュタグ/後半tracklistまで必要なため、軸 run 時のみ大きく渡す。"""
    # 1) uploads playlist を取得
    ch_resp = youtube.channels().list(part="contentDetails,snippet", id=channel_id).execute()
    ch_items = ch_resp.get("items", [])
    if not ch_items:
        return [], ""
    uploads_id = ch_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ch_name = ch_items[0]["snippet"]["title"]

    # 2) playlist items で videoId を取得
    video_ids = []
    page_token = None
    while len(video_ids) < max_results:
        pl_resp = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_id,
            maxResults=min(50, max_results - len(video_ids)),
            pageToken=page_token,
        ).execute()
        for item in pl_resp.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        page_token = pl_resp.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return [], ch_name

    # 3) videos().list で details 取得（50件ずつ）
    videos = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        v_resp = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=",".join(chunk),
        ).execute()
        for v in v_resp.get("items", []):
            stats = v.get("statistics", {})
            snip = v.get("snippet", {})
            videos.append({
                "videoId": v["id"],
                "title": snip.get("title", ""),
                "description": (snip.get("description") or "")[:desc_limit],
                "tags": snip.get("tags", [])[:15],
                "publishedAt": snip.get("publishedAt", ""),
                "viewCount": int(stats.get("viewCount", 0)),
                "likeCount": int(stats.get("likeCount", 0)),
                "commentCount": int(stats.get("commentCount", 0)),
                "duration": v.get("contentDetails", {}).get("duration", ""),
                "channelTitle": snip.get("channelTitle", ""),
            })

    return videos, ch_name


def fetch_competitor_data(rival_urls: list[str], desc_limit: int = 500) -> dict:
    """全ライバルチャンネルの動画を取得して分析用データにまとめる。

    desc_limit: 説明文切り詰め（既定500=従来）。投稿文軸の full 再取得時のみ大きく渡す。"""
    youtube = _get_youtube_service()
    result = {"channels": []}

    for url in rival_urls:
        url = url.strip()
        if not url:
            continue
        print(f"  📡 {url}")
        try:
            ch_id = _extract_channel_id(youtube, url)
            if not ch_id:
                print("    ⚠️ channelId 解決失敗")
                continue
            videos, ch_name = _fetch_channel_videos(youtube, ch_id, max_results=50, desc_limit=desc_limit)
            if not videos:
                print("    ⚠️ 動画なし")
                continue

            # 再生数 TOP10
            by_views = sorted(videos, key=lambda v: v["viewCount"], reverse=True)[:10]
            # 直近投稿 10 本
            by_date = sorted(videos, key=lambda v: v["publishedAt"], reverse=True)[:10]

            result["channels"].append({
                "url": url,
                "channelId": ch_id,
                "channelName": ch_name,
                "totalVideos": len(videos),
                "topByViews": by_views,
                "recentUploads": by_date,
            })
            print(f"    ✓ {ch_name}: {len(videos)} 動画取得 (TOP10 最大再生: {by_views[0]['viewCount']:,})")
        except Exception as e:
            print(f"    ❌ {e}")

    return result


# ─── Claude CLI 分析 ─────────────────────────────────

def _extract_json_object(text: str):
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
    cli_path = shutil.which(cli_cmd) or cli_cmd
    try:
        proc = subprocess.run(
            [cli_path, "-p", prompt],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError(f"claude CLI が見つかりません: {cli_cmd}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI タイムアウト (300s)")

    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI エラー: {(proc.stderr or proc.stdout or '')[:300]}")

    obj = _extract_json_object(proc.stdout)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {proc.stdout[:300]}")

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
    cli_path = shutil.which(cli_cmd) or cli_cmd
    proc = subprocess.run(
        [cli_path, "-p", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI エラー: {(proc.stderr or proc.stdout or '')[:300]}")

    obj = _extract_json_object(proc.stdout)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {proc.stdout[:300]}")

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
    cli_path = shutil.which(cli_cmd) or cli_cmd
    try:
        proc = subprocess.run(
            [cli_path, "-p", prompt],
            capture_output=True, text=True, timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  ⚠ Claude CLI 失敗。ローカル生成にフォールバック: {e}")
        return _local_suno_prompt(str(e))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        print(f"  ⚠ Claude CLI エラー。ローカル生成にフォールバック: {err}")
        return _local_suno_prompt(err)

    obj = _extract_json_object(proc.stdout)
    if not obj or not obj.get("prompt"):
        reason = f"JSON 抽出失敗: {proc.stdout[:300]}"
        print(f"  ⚠ {reason}。ローカル生成にフォールバック")
        return _local_suno_prompt(reason)

    print("  ✓ SUNO プロンプト提案完了")
    return obj


def propose_flow_prompt(
    analysis: dict,
    current_title: str = "",
    context_hint: str = "",
    cli_cmd: str = DEFAULT_CLI,
) -> dict:
    """competitor 分析の visual_direction を Flow (Nano Banana 2) 向けの英語プロンプト 1 段落に変換"""
    vd = (analysis or {}).get("visual_direction") or {}
    if not vd:
        raise RuntimeError("analysis.visual_direction が空です。競合分析を再実行してください (analysis_outdated)")

    bp = (analysis or {}).get("buzz_patterns") or {}
    normalized_visual = {}
    if normalize_visual_direction:
        normalized_visual = normalize_visual_direction(analysis)
    five_part_prompt = ""
    if build_gpt_image2_prompt:
        five_part_prompt = build_gpt_image2_prompt(
            concept=current_title or context_hint,
            visual_direction=normalized_visual,
            context_hint=context_hint,
            for_flow=True,
        )

    prompt = f"""You craft prompts for Google Flow and GPT Image 2 to generate cinematic photorealistic thumbnail-grade images for a BGM YouTube channel.

Hard constraints (always apply):
- 16:9 landscape, hyper-detailed, accurate anatomy
- Legible signage if text appears
- Kodak Portra 400 aesthetic, shallow DOF
- No watermarks, no channel logos
- Do not copy competitor thumbnails. Extract structure and translate it into a new orzz.-native scene.

=== Visual Direction (reverse-engineered from competitor buzz videos) ===
Color palette: {json.dumps(vd.get('color_palette', []))}
Time of day: {vd.get('time_of_day', '')}
Subjects: {json.dumps(vd.get('subjects', []))}
Composition: {vd.get('composition', '')}
Atmosphere: {vd.get('atmosphere', '')}
Avoid: {json.dumps(vd.get('avoid', []))}

=== Normalized five-part image brief ===
{five_part_prompt or '(unavailable)'}

=== Viewer context ===
Viewer needs (what scene the thumbnail should promise): {json.dumps(bp.get('viewer_needs', []))}

=== Current state ===
Video title: {current_title or '(none)'}
User context hint (may be empty): {context_hint or '(none)'}

=== Your Task ===
Compose prompts that preserve the five basic image-generation elements:
1. Subject: what to draw.
2. Background/context: where it is and why it fits the video promise.
3. Lighting: time of day, light source, shadow quality.
4. Style/rendering: photorealistic / cinematic / material treatment.
5. Camera/composition: framing, lens feel, focal point, aspect ratio.

Return both:
- flow_prompt: one English paragraph, roughly 60-120 words.
- gpt_image2_prompt: a labeled English prompt with Subject / Background-context / Lighting / Style-rendering / Camera-composition / Constraints.

Respond with a SINGLE JSON object:
{{
  "prompt": "<the Flow prompt, one paragraph, in English>",
  "gpt_image2_prompt": "<the GPT Image 2 prompt, labeled and production-ready, in English>",
  "rationale": "<1-2 English sentences explaining which viewer need this scene targets>"
}}

Output ONLY the JSON object.
"""

    print("🖼 Claude CLI で Flow プロンプト提案中...")
    cli_path = shutil.which(cli_cmd) or cli_cmd
    proc = subprocess.run(
        [cli_path, "-p", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI エラー: {(proc.stderr or proc.stdout or '')[:300]}")

    obj = _extract_json_object(proc.stdout)
    if not obj or not obj.get("prompt"):
        raise RuntimeError(f"JSON 抽出失敗: {proc.stdout[:300]}")
    if not obj.get("gpt_image2_prompt") and five_part_prompt:
        obj["gpt_image2_prompt"] = five_part_prompt

    print("  ✓ Flow プロンプト提案完了")
    return obj


def fuse_benchmark_profiles(
    profiles: list,
    cli_cmd: str = DEFAULT_CLI,
) -> dict:
    """複数ベンチマーク・プロファイルを融合し、orzz. 向けの統合 direction を返す。

    受け要素の抽出 → orzz. の世界観への落とし込みを行う。単純な平均ではなく、
    「視聴者に最も刺さる要素」を抽出し、orzz. らしく再解釈する。
    """
    if not profiles:
        raise RuntimeError("profiles が空です")

    import shutil as _sh
    import subprocess as _sp

    extract = []
    for p in profiles:
        pf = (p or {}).get("profile") or {}
        extract.append({
            "channel_name": p.get("channel_name", ""),
            "subscribers": p.get("subscribers", 0),
            "top_videos": [
                {"title": v.get("title", ""), "views": v.get("views", 0), "published": (v.get("published") or v.get("published_at") or "")[:10]}
                for v in (p.get("top_videos") or [])[:5]
            ],
            "recent_videos": [
                {"title": v.get("title", ""), "views": v.get("views", 0), "published": (v.get("published") or v.get("published_at") or "")[:10]}
                for v in (p.get("recent_videos") or [])[:5]
            ],
            "music_profile": pf.get("music_profile", {}),
            "visual_profile": pf.get("visual_profile", {}),
            "persona": pf.get("persona", {}),
            "appeal_points": pf.get("appeal_points", []),
            "buzz_elements": pf.get("buzz_elements", {}),
            "comment_insights": pf.get("comment_insights", []),
        })

    _n = len(extract)
    _scope = ("1 つの競合チャンネルプロファイルから、勝ち筋の要素を抽出して orzz. 向けに翻案する"
              if _n == 1 else
              f"{_n} 件の競合チャンネルプロファイルを 1 つの統合 direction に融合する")
    prompt = f"""あなたは YouTube BGM チャンネル「orzz.」の制作ディレクターです。{_scope}のがタスクです。

=== 大原則（厳守）===
競合のコピー・クローンではありません。目的は **視聴者に刺さる要素（何が視聴者を惹きつけているか）を抽出**し、**orzz. 独自の世界観へ翻案**すること。商標的な画像・音を再現しないこと。固有名詞は禁止（実在アーティスト名・実在曲/アルバム名・チャンネル固有のキャッチフレーズは使わない）。

=== 言語ルール（厳守・最重要）===
- **分析・方向性・戦略の説明文は日本語**で書く: rationale / shared_hooks / fused_music_direction(mood_tags, reference_vibe, avoid) / fused_visual_direction(全項目) / fused_persona(全項目) / buzz_to_prompt_translation.title_angles / orzz_adaptation(keep/transform/avoid)。
  - ジャンル名・楽器名・配色名など定着した英語表記は日本語文中にそのまま含めてよい（例: 「Instrumental Jazz Hop」「Rhodes」）。
- **生成器に渡す/ YouTube 出力メタは英語**で書く: title_candidates（YouTube タイトル）/ suno_prompt / buzz_to_prompt_translation.suno_prompt_ingredients / flow_prompt / gpt_image2_prompt_template / image_prompts / buzz_to_prompt_translation.image_prompt_ingredients。
- 日本語フィールドに英語の長文説明を書かない／英語フィールドに日本語を書かない。

=== 競合プロファイル ===
{json.dumps(extract, ensure_ascii=False, indent=2)}

=== タスク ===
{'このチャンネルの' if _n == 1 else 'これらのプロファイルを比較し、'}再生上位動画と直近投稿を見比べて「今も効いている要素」を特定。バズ要素を抽出し、orzz. ネイティブな direction に再構成して、タイトル・SUNO プロンプト・画像生成プロンプトを直接ドライブできる形にする。

次の単一 JSON オブジェクトのみで回答（上記の言語ルール厳守）:
{{
  "fused_music_direction": {{
    "recommended_genres": ["ジャンル名（英語表記可）"],
    "bpm_range": {{"min": 60, "max": 85}},
    "mood_tags": ["ムードを日本語で"],
    "instrumentation": ["楽器（英語表記可）"],
    "reference_vibe": "<印象的なシーンを1文・日本語>",
    "avoid": ["避けるべき要素を日本語で"]
  }},
  "fused_visual_direction": {{
    "color_palette": ["配色を日本語で（色名は一般語可）"],
    "time_of_day": "時間帯（日本語）",
    "subjects": ["被写体を日本語で"],
    "composition": "構図（日本語）",
    "atmosphere": "雰囲気（日本語）",
    "avoid": ["避けるべき要素を日本語で"]
  }},
  "fused_persona": {{
    "age_range": "年齢層（日本語）",
    "viewing_scenes": ["視聴シーンを日本語で"],
    "psychological_needs": ["心理的ニーズを日本語で"]
  }},
  "shared_hooks": ["<複数競合に共通する視聴者訴求要素を日本語で>", "..."],
  "buzz_to_prompt_translation": {{
    "title_angles": ["<タイトルで使う視聴者の瞬間/約束を日本語で>"],
    "suno_prompt_ingredients": ["<English: genre, tempo, instruments, texture, scene>"],
    "image_prompt_ingredients": ["<English: scene, palette, lighting, composition>"]
  }},
  "orzz_adaptation": {{
    "keep": ["<そのまま採用すべき要素を日本語で>"],
    "transform": ["<orzz. の世界観へ翻案すべき要素を日本語で>"],
    "avoid": ["<orzz. ブランドと衝突する競合特性を日本語で>"]
  }},
  "title_candidates": ["<English YouTube title under 60 chars>", "..."],
  "suno_prompt": "<a SINGLE-LINE SUNO prompt (150-250 chars, English). No real artist/song names. No vocals. Lead with genre+mood+instruments+BPM. Anchor one sensory scene.>",
  "flow_prompt": "<ONE English paragraph (60-120 words) for Google Flow thumbnail. 16:9, Kodak Portra 400, no watermarks, no logos. Compose scene + palette + lighting + composition.>",
  "gpt_image2_prompt_template": "<labeled English GPT Image 2 prompt using Subject, Background/context, Lighting, Style/rendering, Camera/composition, Constraints>",
  "image_prompts": ["<English GPT Image 2 prompt variant 1 using the five-part structure>", "<variant 2>", "<variant 3>", "<variant 4>"],
  "rationale": "<この方針が orzz. になぜ効くか・どの視聴者ニーズを狙うかを日本語で2〜3文>"
}}

Output ONLY the JSON object.
"""

    print(f"🧬 Claude CLI で {len(profiles)} プロファイルを融合中...")
    cli_path = _sh.which(cli_cmd) or cli_cmd
    proc = _sp.run(
        [cli_path, "-p", prompt],
        capture_output=True, text=True, timeout=420,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI エラー: {(proc.stderr or proc.stdout or '')[:300]}")

    obj = _extract_json_object(proc.stdout)
    if not obj or not obj.get("suno_prompt"):
        raise RuntimeError(f"JSON 抽出失敗: {proc.stdout[:400]}")

    print("  ✓ 融合完了")
    return obj


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
    cli_path = shutil.which(cli_cmd) or cli_cmd
    # 画像ファイルを読めるように親ディレクトリを --add-dir で許可
    add_dirs = []
    seen_dirs = set()
    for p in local_paths:
        parent = str(p.parent)
        if parent not in seen_dirs:
            add_dirs.extend(["--add-dir", parent])
            seen_dirs.add(parent)

    proc = subprocess.run(
        [cli_path, "-p", prompt, *add_dirs],
        capture_output=True, text=True, timeout=600,
    )

    # tmpクリーンアップ
    if tmp_dir:
        try:
            for f in downloaded:
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass

    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI エラー: {(proc.stderr or proc.stdout or '')[:300]}")

    obj = _extract_json_object(proc.stdout)
    if not obj or not obj.get("element_extraction"):
        raise RuntimeError(f"JSON 抽出失敗: {proc.stdout[:300]}")

    print(f"  ✓ サムネ要素抽出完了（{len(local_paths)} 枚）")
    return obj


def import_benchmark_from_sheet(sheet_url: str) -> dict:
    """Google Sheets URL から全シート/全タブを取得し、ベンチマーク登録候補を抽出する。

    シートの構造は任意。各タブの内容を読み取り、チャンネル名/URL/視聴数などに見える列を
    Claude CLI 側で解釈する前提の raw データとして返す。

    Args:
        sheet_url: https://docs.google.com/spreadsheets/d/{id}/edit... 形式のURL

    Returns:
        {"sheet_id": str, "tabs": [{"name": str, "rows": [[...], ...]}]}
    """
    import urllib.request
    import urllib.parse

    # URL から ID 抽出
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet_url)
    if not m:
        raise RuntimeError(f"Sheets URL から ID を抽出できません: {sheet_url}")
    sheet_id = m.group(1)

    # export?format=csv で全シートを一度に取るのは不可。gid=0 以外は個別取得が必要。
    # 簡易対応: デフォルト1枚目のみ CSV で取得。複数タブ対応は Sheets API 認証要。
    # dashboard_config > ~/.config/orzz/sheets_api_key.txt の順で解決
    api_key = ""
    try:
        dc_path = Path.home() / ".config" / "orzz" / "dashboard_config.json"
        if dc_path.exists():
            dc = json.loads(dc_path.read_text(encoding="utf-8"))
            api_key = (dc.get("sheets_api_key") or "").strip()
    except Exception:
        pass
    if not api_key:
        api_key_file = Path.home() / ".config" / "orzz" / "sheets_api_key.txt"
        if api_key_file.exists():
            api_key = api_key_file.read_text(encoding="utf-8").strip()
    if api_key:
        # メタデータ取得で全シート列挙
        meta_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}?key={api_key}"
        meta = json.loads(urllib.request.urlopen(meta_url, timeout=30).read())
        tabs = []
        for sh in meta.get("sheets", []):
            title = sh["properties"]["title"]
            rng_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(title)}?key={api_key}"
            try:
                data = json.loads(urllib.request.urlopen(rng_url, timeout=30).read())
                tabs.append({"name": title, "rows": data.get("values", [])})
            except Exception as e:
                tabs.append({"name": title, "rows": [], "error": str(e)})
        return {"sheet_id": sheet_id, "tabs": tabs, "mode": "api"}

    # フォールバック: CSV export（1枚目のみ）
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    try:
        raw = urllib.request.urlopen(csv_url, timeout=30).read().decode("utf-8", errors="replace")
        import csv, io
        rows = list(csv.reader(io.StringIO(raw)))
        return {"sheet_id": sheet_id, "tabs": [{"name": "Sheet1", "rows": rows}], "mode": "csv_public",
                "hint": "Sheets API キー未設定のため1枚目のみ取得。全シート取得は ~/.config/orzz/sheets_api_key.txt に API キーを保存してください"}
    except Exception as e:
        raise RuntimeError(f"Sheets 公開CSV取得失敗（非公開シートの可能性）: {e}")


# ─── メイン ──────────────────────────────────────────

def run_full_analysis(cli_cmd: str = DEFAULT_CLI) -> dict:
    """ライバルチャンネル優先 → スプシフォールバック → Claude 分析 → キャッシュ保存

    優先順位:
      1) rival_channels が登録されていれば YouTube API で取得（ユーザー意図を尊重）。
         スプシも設定済みなら growth_summary だけは別途スプシから取得して文脈に合成。
      2) rival_channels が空なら、スプシ（適用中ベンチマーク）から取得。
      3) どちらも空ならエラー。
    """
    cfg = _load_analysis_config()

    print("=" * 60)
    print("  競合チャンネル分析")
    print("=" * 60)

    competitor_data = None
    growth_summary = None
    source = "unknown"

    detail_url = cfg.get("spreadsheet_channel_detail_url", "").strip()
    growth_url = cfg.get("spreadsheet_growth_tracking_url", "").strip()
    rivals = cfg.get("rival_channels") or []
    print(f"  設定: rivals={len(rivals)} / detail_url={'set' if detail_url else 'empty'} / growth_url={'set' if growth_url else 'empty'}")

    # 1) ライバルチャンネル優先
    if rivals:
        print(f"\n📡 ライバルチャンネルを YouTube API で取得（{len(rivals)} 件）")
        try:
            competitor_data = fetch_competitor_data(rivals)
            if competitor_data and competitor_data.get("channels"):
                source = "youtube_api_rivals"
                print(f"  ✅ {len(competitor_data['channels'])} チャンネル取得成功")
            else:
                competitor_data = None
                print("  ⚠️ ライバル取得 0 件 → スプシフォールバック")
        except Exception as e:
            print(f"  ⚠️ ライバル取得失敗: {e} → スプシフォールバック")
            competitor_data = None

        # growth_summary はスプシ由来のみ。rival 優先時もシグナルとして付与する。
        if competitor_data and detail_url and growth_url:
            try:
                from app_sheets import fetch_from_spreadsheets
                bench_filter = cfg.get("benchmark_filter", {}) or {}
                _spsh, growth_summary = fetch_from_spreadsheets(
                    detail_url, growth_url,
                    top_n=int(bench_filter.get("top_n", 15)),
                    pinned_names=cfg.get("benchmark_pinned_names") or None,
                    min_subs=int(bench_filter.get("min_subs", 0)),
                    max_subs=bench_filter.get("max_subs"),
                    exclude_names=bench_filter.get("exclude_names") or None,
                )
                if growth_summary:
                    print(f"  ℹ️ スプシから growth_summary を補助取得（hot {len((growth_summary or {}).get('hot_channels') or [])} 件）")
            except Exception as e:
                print(f"  ⚠️ growth_summary 取得失敗（無視して続行）: {e}")
                growth_summary = None

    # 2) スプシフォールバック（rivals 空 or rival 取得失敗時）
    if not competitor_data:
        if not (detail_url and growth_url):
            raise RuntimeError("データソースが設定されていません（ライバルチャンネル または スプシ URL のどちらかが必要）")
        print("\n📊 スプレッドシートからデータ取得（API quota ゼロ）")
        try:
            from app_sheets import fetch_from_spreadsheets
            bench_filter = cfg.get("benchmark_filter", {}) or {}
            competitor_data, growth_summary = fetch_from_spreadsheets(
                detail_url, growth_url,
                top_n=int(bench_filter.get("top_n", 15)),
                pinned_names=cfg.get("benchmark_pinned_names") or None,
                min_subs=int(bench_filter.get("min_subs", 0)),
                max_subs=bench_filter.get("max_subs"),
                exclude_names=bench_filter.get("exclude_names") or None,
            )
        except Exception as e:
            raise RuntimeError(f"スプシ取得失敗: {e}")
        if not competitor_data or not competitor_data.get("channels"):
            raise RuntimeError("競合データの取得に失敗しました（スプシも空）")
        source = "spreadsheet"
        print(f"  ✅ {len(competitor_data['channels'])} チャンネル取得成功")

    # Claude で分析（growth_summary があれば注入）
    analysis = analyze_with_claude(competitor_data, cli_cmd, growth_summary=growth_summary)

    # キャッシュ保存
    cache = {
        "competitor_data": competitor_data,
        "analysis": analysis,
        "source": source,
        "analyzed_at": __import__("datetime").datetime.now().isoformat(),
        "language": "ja",
        "prompt_version": 5,
    }
    if growth_summary:
        cache["growth_summary"] = growth_summary
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 キャッシュ保存: {CACHE_FILE} (source: {source}, language: ja, v5)")
    return cache


def load_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="orzz. 競合分析")
    parser.add_argument("--analyze", action="store_true", help="分析のみ実行")
    parser.add_argument("--propose", type=int, help="vol 番号を指定して提案まで実行")
    parser.add_argument("--cli", default=DEFAULT_CLI, help="claude CLI コマンド")
    args = parser.parse_args()

    if args.analyze or args.propose:
        cache = run_full_analysis(cli_cmd=args.cli)
        if args.propose:
            # フォルダ解決
            cfg = json.loads((CONFIG_DIR / "dashboard_config.json").read_text(encoding="utf-8"))
            ch_dir = Path(cfg.get("channel_folder", ""))
            prefix = re.sub(r"[^A-Za-z0-9_-]+", "", str(cfg.get("file_prefix") or "vol")) or "vol"
            pat = re.compile(rf"^({args.propose})_(?:{re.escape(prefix)}|orzz)(?:_|$)")
            folder = None
            for d in ch_dir.iterdir():
                if d.is_dir() and pat.match(d.name):
                    folder = d
                    break
            if not folder:
                print(f"❌ vol.{args.propose} のフォルダが見つかりません")
                sys.exit(1)
            # コンテキスト収集
            songs = []
            music_dir = folder / "music"
            if music_dir.is_dir():
                songs = [p.stem for p in sorted(music_dir.glob("*.mp3"))]
            title_file = folder / "youtube_title.txt"
            current_title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""

            result = propose_with_analysis(
                cache["analysis"], cache["competitor_data"],
                cli_cmd=args.cli, current_title=current_title,
                songs=songs, persona=cfg.get("persona", ""),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


# ─── ベンチマーク完成パイプライン（API key ベース・全自動） ──────────

BENCHMARK_PROFILES_FILE = CONFIG_DIR / "benchmark_profiles.json"


def _get_dashboard_cfg() -> dict:
    p = CONFIG_DIR / "dashboard_config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_youtube_api_key() -> str:
    """dashboard_config.youtube_api_key > env > ~/.config/orzz/youtube_api_key.txt の順で解決"""
    import os
    cfg = _get_dashboard_cfg()
    key = (cfg.get("youtube_api_key") or "").strip()
    if key:
        return key
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    fp = CONFIG_DIR / "youtube_api_key.txt"
    if fp.exists():
        return fp.read_text(encoding="utf-8").strip()
    return ""


def _resolve_sheets_api_key() -> str:
    cfg = _get_dashboard_cfg()
    k = (cfg.get("sheets_api_key") or "").strip()
    if k:
        return k
    fp = CONFIG_DIR / "sheets_api_key.txt"
    if fp.exists():
        return fp.read_text(encoding="utf-8").strip()
    return ""


def _extract_video_id(url: str) -> str:
    """YouTube 動画 URL から videoId を抽出"""
    m = re.search(r"(?:v=|/shorts/|/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def fetch_video_comments_by_key(video_id: str, api_key: str, max_results: int = 50) -> list:
    """API key で commentThreads を取得。失敗時は空配列"""
    import urllib.request, urllib.parse
    if not video_id or not api_key:
        return []
    try:
        qs = urllib.parse.urlencode({
            "part": "snippet",
            "videoId": video_id,
            "maxResults": min(100, max_results),
            "order": "relevance",
            "textFormat": "plainText",
            "key": api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/commentThreads?{qs}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        out = []
        for item in data.get("items", []):
            sn = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            out.append({
                "author": sn.get("authorDisplayName", ""),
                "text": sn.get("textDisplay") or sn.get("textOriginal") or "",
                "likes": sn.get("likeCount", 0),
                "published": sn.get("publishedAt", ""),
            })
        return out
    except Exception as e:
        print(f"    ⚠️ コメント取得失敗 {video_id}: {e}")
        return []


def _fetch_video_details_by_key(video_ids: list[str], api_key: str) -> list[dict]:
    """video ids から snippet/statistics/contentDetails をまとめて取得"""
    import urllib.request, urllib.parse
    ids = [v for v in dict.fromkeys(video_ids or []) if v]
    if not ids or not api_key:
        return []
    out = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        qs = urllib.parse.urlencode({
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(chunk),
            "key": api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/videos?{qs}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        for v in data.get("items", []):
            sn = v.get("snippet", {})
            st = v.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            thumb_url = (thumbs.get("maxres") or thumbs.get("high") or
                         thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            out.append({
                "video_id": v.get("id", ""),
                "title": sn.get("title", ""),
                "description": (sn.get("description") or "")[:500],
                "published": sn.get("publishedAt", ""),
                "published_at": sn.get("publishedAt", ""),
                "thumbnail": thumb_url,
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments_count": int(st.get("commentCount", 0)),
                "duration": v.get("contentDetails", {}).get("duration", ""),
                "url": f"https://www.youtube.com/watch?v={v.get('id', '')}",
                "tags": sn.get("tags", [])[:15],
            })
    return out


def _fetch_recent_uploads_by_key(channel_item: dict, api_key: str, max_results: int = 10) -> list[dict]:
    """channels.list の item から uploads playlist をたどり、最新投稿を取得"""
    import urllib.request, urllib.parse
    uploads = (
        channel_item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads:
        return []
    video_ids = []
    page_token = ""
    while len(video_ids) < max_results:
        params = {
            "part": "contentDetails",
            "playlistId": uploads,
            "maxResults": min(50, max_results - len(video_ids)),
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        qs = urllib.parse.urlencode(params)
        url = f"https://www.googleapis.com/youtube/v3/playlistItems?{qs}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return _fetch_video_details_by_key(video_ids[:max_results], api_key)


def fetch_channel_basic_by_key(channel_url: str, api_key: str) -> dict:
    """チャンネル URL から基本情報 + 最新/人気動画を API key で取得"""
    import urllib.request, urllib.parse
    if not api_key:
        raise RuntimeError("YouTube API key が未設定")
    cid = None
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", channel_url)
    if m:
        cid = m.group(1)
    else:
        handle_m = re.search(r"/(@[\w\-.]+)", channel_url)
        if handle_m:
            handle = handle_m.group(1)
            qs = urllib.parse.urlencode({"part": "id,snippet,statistics",
                                         "forHandle": handle, "key": api_key})
            url = f"https://www.googleapis.com/youtube/v3/channels?{qs}"
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.loads(r.read())
            if d.get("items"):
                cid = d["items"][0]["id"]
    if not cid:
        raise RuntimeError(f"channelId が解決できません: {channel_url}")

    # 基本情報
    qs = urllib.parse.urlencode({"part": "snippet,statistics,contentDetails",
                                 "id": cid, "key": api_key})
    url = f"https://www.googleapis.com/youtube/v3/channels?{qs}"
    with urllib.request.urlopen(url, timeout=20) as r:
        d = json.loads(r.read())
    if not d.get("items"):
        raise RuntimeError(f"チャンネル情報が取れません: {cid}")
    ch = d["items"][0]
    sn = ch.get("snippet", {})
    stat = ch.get("statistics", {})

    # 人気動画 TOP10 (search API)
    qs = urllib.parse.urlencode({"part": "id,snippet", "channelId": cid,
                                 "order": "viewCount", "maxResults": 10,
                                 "type": "video", "key": api_key})
    url = f"https://www.googleapis.com/youtube/v3/search?{qs}"
    with urllib.request.urlopen(url, timeout=20) as r:
        s = json.loads(r.read())
    video_ids = [it["id"]["videoId"] for it in s.get("items", []) if it.get("id", {}).get("videoId")]
    videos = _fetch_video_details_by_key(video_ids, api_key)
    recent_videos = _fetch_recent_uploads_by_key(ch, api_key, max_results=10)

    return {
        "channel_id": cid,
        "channel_name": sn.get("title", ""),
        "description": sn.get("description", ""),
        "subscribers": int(stat.get("subscriberCount", 0)),
        "total_views": int(stat.get("viewCount", 0)),
        "video_count": int(stat.get("videoCount", 0)),
        "url": f"https://www.youtube.com/channel/{cid}",
        "thumbnail": (sn.get("thumbnails", {}).get("high") or
                      sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
        "top_videos": videos,
        "recent_videos": recent_videos,
    }


def refresh_channel_with_youtube_api(ch: dict, api_key: str) -> dict:
    """Sheet由来のチャンネルも、分析直前に Data API の最新動画/再生数で上書きする"""
    url = (ch or {}).get("url") or ""
    if not api_key or not url:
        return ch
    meta = fetch_channel_basic_by_key(url, api_key)
    keep = {
        "growth": ch.get("growth"),
        "source": ch.get("source", "sheet_a"),
    }
    refreshed = {**ch, **meta}
    refreshed.update({k: v for k, v in keep.items() if v is not None})
    refreshed["source"] = f"{keep.get('source') or 'sheet_a'}+youtube_api"
    return refreshed


def build_channel_profile(channel_meta: dict, comments_by_video: dict = None,
                          cli_cmd: str = DEFAULT_CLI) -> dict:
    """Claude CLI でチャンネル単位の統合プロファイルを生成。

    - 楽曲プロファイル（ジャンル/BPM 推定/雰囲気/楽器/イメージ）
    - 視覚プロファイル（配色/構図/時間帯/被写体/共通要素）
    - ペルソナ（年齢層/視聴シーン/心理的ニーズ/訴求点）
    - コメントからの洞察
    """
    ch_name = channel_meta.get("channel_name", "")
    subs = channel_meta.get("subscribers", 0)
    desc = (channel_meta.get("description") or "")[:400]
    videos = channel_meta.get("top_videos", [])
    recent_videos = channel_meta.get("recent_videos", [])
    summary_lines = [f"=== {ch_name} (subs: {subs:,}) ==="]
    if desc:
        summary_lines.append(f"[Description]\n{desc}")
    summary_lines.append("\n[TOP videos]")
    for i, v in enumerate(videos, 1):
        summary_lines.append(f"{i}. [{v.get('views', 0):,} views] {v.get('title', '')}")
        if v.get("thumbnail"):
            summary_lines.append(f"   thumbnail: {v['thumbnail']}")
    if recent_videos:
        summary_lines.append("\n[Latest uploads with current view counts]")
        for i, v in enumerate(recent_videos[:10], 1):
            published = (v.get("published") or v.get("published_at") or "")[:10]
            summary_lines.append(f"{i}. [{v.get('views', 0):,} views | {published}] {v.get('title', '')}")
    comments_by_video = comments_by_video or {}
    if comments_by_video:
        summary_lines.append("\n[Top comments (viewer voices)]")
        for vid, clist in comments_by_video.items():
            if not clist:
                continue
            title = next((v["title"] for v in videos if v.get("video_id") == vid), vid)
            summary_lines.append(f"\n<Video: {title[:60]}>")
            for c in clist[:8]:
                txt = (c.get("text") or "").replace("\n", " ")[:200]
                summary_lines.append(f"  · [{c.get('likes', 0)}👍] {txt}")

    prompt = f"""あなたは BGM / インストゥルメンタル系 YouTube チャンネル分析の専門家です。次のチャンネルを分析し、構造化されたベンチマークプロフィールを JSON で返してください。

{chr(10).join(summary_lines)}

分析観点:
- 再生数上位動画: すでにクリック・リピートが証明されている要素
- 最新投稿: チャンネルが今試している方向性と現在の反応
- 視聴者コメント: 視聴者が聴き続ける明示的な理由

自チャンネルのタイトル、SUNOプロンプト、画像生成プロンプトに翻訳できるバズ要素を抽出してください。

言語: JSON 内の説明文・分析文・提案文はすべて自然な日本語で書いてください。ジャンル名や楽器名に一般的な英語表記が必要な場合だけ、日本語説明の中に短く含めて構いません。

次の形の JSON オブジェクトのみを返してください（コードフェンスや前後の文章なし）。例の値は説明用です。実際のチャンネルに合わせた日本語の値を入れてください:

{{
  "music_profile": {{
    "genres": ["ローファイ・ヒップホップ", "ジャズピアノ"],
    "bpm_range": {{"min": 60, "max": 80}},
    "mood": ["温かい", "もの寂しい"],
    "instrumentation": ["ローズピアノ", "柔らかいドラム", "テープヒス"],
    "imagery": "雨の深夜カフェ、窓に柔らかいネオンがにじむ一人の静かな時間",
    "energy": "低め",
    "vocals": "インストゥルメンタル"
  }},
  "visual_profile": {{
    "palette": ["深い琥珀色", "ネイビー", "暖かいセピア"],
    "time_of_day": "深夜 / ブルーアワー",
    "composition": "ワイドショット、浅い被写界深度、ローキー照明",
    "recurring_subjects": ["一人で過ごす人物", "雨の窓", "閉店後のカフェ"],
    "atmosphere": "静かで内省的、シネマティック"
  }},
  "persona": {{
    "age_range": "25-40",
    "demographics": "都市部のクリエイティブワーカーやリモートワーカー",
    "viewing_scenes": ["深夜の勉強", "在宅ワーク中の集中", "寝る前のクールダウン"],
    "psychological_needs": ["気持ちのリセット", "一人時間に寄り添われる感覚", "生産的な集中"],
    "gender_skew": "やや女性寄り / 中立"
  }},
  "appeal_points": [
    "一貫した視覚世界の作り込み",
    "長時間途切れず聴けるミックス",
    "リスナーの具体的な瞬間を名指す共感的なタイトル"
  ],
  "buzz_elements": {{
    "title_hooks": ["具体的なシーンとリスナーの瞬間を約束するタイトル"],
    "music_hooks": ["SUNOプロンプトに翻訳できる音色、テンポ、楽器、空気感"],
    "visual_hooks": ["画像生成プロンプトに翻訳できるサムネのシーン、色、構図"],
    "latest_signal": "最新投稿から見える現在のトレンドと反応温度"
  }},
  "comment_insights": [
    "視聴者は深夜の勉強や仕事のために使っている",
    "many comments express gratitude for easing anxiety",
    "some viewers ask for longer continuous mixes"
  ],
  "adaptation_hints_for_orzz": {{
    "keep": ["cinematic rainy mood", "one-hour-plus long-form format"],
    "transform": ["make the piano tone slightly warmer for orzz.'s identity", "add subtle tape texture"],
    "avoid": ["bright pastel colors", "upbeat electronic drums", "vocals"]
  }}
}}

IMPORTANT: The "adaptation_hints_for_orzz" field is for translating what makes this channel work into orzz.'s own aesthetic. DO NOT suggest copying or reproducing the channel's exact assets. Focus on extractable *principles* and *viewer appeal mechanisms*. All values MUST be in English.
"""

    try:
        proc = subprocess.run(
            [cli_cmd, "-p", prompt],
            capture_output=True, text=True, timeout=300,
        )
        out = proc.stdout.strip()
        parsed = _extract_json_object(out)
        if not parsed:
            return {"error": "JSON解析失敗", "raw": out[:500]}
        return parsed
    except subprocess.TimeoutExpired:
        return {"error": "Claude タイムアウト"}
    except Exception as e:
        return {"error": str(e)}


def _parse_sheet_a_rows(rows: list) -> list:
    """Sheet A（チャンネル詳細）の行から構造化チャンネル情報を抽出"""
    if not rows or len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]
    col = {h: i for i, h in enumerate(header)}
    channels = []
    for row in rows[1:]:
        if not row or len(row) < 3:
            continue
        def g(key, default=""):
            i = col.get(key)
            if i is None or i >= len(row):
                return default
            return row[i]
        url = g("URL")
        name = g("TITLE")
        # URL が http で始まらない行はヘッダ/日本語ラベル行としてスキップ
        if not url.startswith("http"):
            continue
        subs = g("SUBSCRIBERS", "0").replace(",", "")
        try:
            subs_i = int(subs) if subs else 0
        except ValueError:
            subs_i = 0
        total = g("TOTAL_VIEWS", "0").replace(",", "")
        try:
            total_i = int(total) if total else 0
        except ValueError:
            total_i = 0
        top_videos = []
        for i in range(1, 6):
            suf = "" if i == 1 else str(i)
            v_url = g(f"TOP_VIDEO_URL{suf}")
            v_title = g(f"TOP_VIDEO_TITLE{suf}")
            v_thumb = g(f"TOP_VIDEO_THUMBNAIL{suf}")
            v_views = g(f"TOP_VIDEO_VIEWS{suf}", "0").replace(",", "")
            if not v_url and not v_title:
                continue
            try:
                v_views_i = int(v_views) if v_views else 0
            except ValueError:
                v_views_i = 0
            top_videos.append({
                "url": v_url,
                "title": v_title,
                "thumbnail": v_thumb,
                "views": v_views_i,
                "video_id": _extract_video_id(v_url),
            })
        channels.append({
            "channel_name": name,
            "url": url,
            "subscribers": subs_i,
            "total_views": total_i,
            "description": g("DESCRIPTION"),
            "thumbnail": g("ICON_IMAGE"),
            "top_videos": top_videos,
            "source": "sheet_a",
        })
    return channels


def _parse_sheet_b_rows(rows: list) -> dict:
    """Sheet B (成長トラッキング) の行から {name: {...}} を抽出。
    列: 0=アイコン 1=チャンネル名 2=個別シート 3=追跡開始日 4=取得日時
         5=総再生回数 6=登録者数 7=前日比再生数 8=前日比登録者数 9=直近伸び率"""
    if not rows or len(rows) < 3:
        return {}
    out = {}
    for row in rows[2:]:
        if not row or len(row) < 2:
            continue
        name = (row[1] if len(row) > 1 else "").strip()
        if not name:
            continue
        def _pf(idx):
            if idx >= len(row):
                return 0
            s = (row[idx] or "").replace(",", "").replace("%", "").strip()
            try:
                return float(s) if s else 0
            except ValueError:
                return 0
        def _s(idx):
            if idx >= len(row):
                return ""
            return (row[idx] or "").strip()
        out[name] = {
            "tracking_start": _s(3),
            "fetched_at": _s(4),
            "total_views": int(_pf(5)),
            "total_subs": int(_pf(6)),
            "views_diff": int(_pf(7)),
            "subs_diff": int(_pf(8)),
            "growth_rate": _pf(9),
        }
    return out


def list_benchmark_sources(sheet_a_url: str = "", sheet_b_url: str = "",
                           extra_urls: list = None) -> dict:
    """Sheet A/B + extra_urls からチャンネル一覧（名前・サブスク・成長・サムネ）のみを返す。
    Claude は呼ばない軽量版。ユーザーが選択画面で使う。"""
    extra_urls = [u.strip() for u in (extra_urls or []) if u.strip()]
    all_channels = []

    if sheet_a_url:
        try:
            sa = import_benchmark_from_sheet(sheet_a_url)
            for tab in sa.get("tabs", []):
                chs = _parse_sheet_a_rows(tab.get("rows", []))
                if chs:
                    all_channels.extend(chs)
                    break
        except Exception as e:
            print(f"Sheet A 取得失敗: {e}")

    growth_map = {}
    if sheet_b_url:
        try:
            sb = import_benchmark_from_sheet(sheet_b_url)
            for tab in sb.get("tabs", []):
                m = _parse_sheet_b_rows(tab.get("rows", []))
                if m:
                    growth_map.update(m)
        except Exception as e:
            print(f"Sheet B 取得失敗: {e}")

    for ch in all_channels:
        g = growth_map.get(ch["channel_name"])
        if g:
            ch["growth"] = g

    api_key = _resolve_youtube_api_key()
    for url in extra_urls:
        try:
            meta = fetch_channel_basic_by_key(url, api_key) if api_key else {"channel_name": url, "url": url}
            meta["source"] = "extra_url"
            all_channels.append(meta)
        except Exception as e:
            all_channels.append({"channel_name": url, "url": url, "source": "extra_url", "error": str(e)})

    # 既存プロファイルと突合（S2: 分析済み可視化＝再分析の温床を断つ）
    analyzed = {}  # key -> {generated_at, valid}
    payload_generated_at = ""
    if BENCHMARK_PROFILES_FILE.exists():
        try:
            bp = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
            payload_generated_at = bp.get("generated_at", "") or ""
            for p in (bp.get("profiles") or []):
                k = _bm_profile_key(p.get("channel_name"), p.get("url"))
                prof = p.get("profile")
                analyzed[k] = {
                    "generated_at": p.get("_analyzed_at") or payload_generated_at,
                    "valid": isinstance(prof, dict) and not prof.get("error"),
                }
        except Exception:
            analyzed = {}

    out = []
    for ch in all_channels:
        k = _bm_profile_key(ch.get("channel_name"), ch.get("url"))
        a = analyzed.get(k)
        out.append({
            "channel_name": ch.get("channel_name", ""),
            "url": ch.get("url", ""),
            "subscribers": ch.get("subscribers", 0),
            "total_views": ch.get("total_views", 0),
            "thumbnail": ch.get("thumbnail", ""),
            "growth": ch.get("growth"),
            "source": ch.get("source", "unknown"),
            "top_video_count": len(ch.get("top_videos") or []),
            "already_analyzed": bool(a and a["valid"]),
            "last_analyzed_at": (a or {}).get("generated_at", ""),
        })
    return {"channels": out}


def _bm_norm_url(url: str = "") -> str:
    """チャンネル URL を実体識別子へ正規化（末尾タブ /videos 等を除去）。"""
    u = (url or "").strip().lower().rstrip("/")
    if not u:
        return ""
    u = re.sub(r"/(videos|featured|about|streams|community|playlists|shorts)$", "", u)
    return u


def _bm_profile_key(channel_name: str = "", url: str = "") -> str:
    """プロファイルの一意キー。channel_name 優先・無ければ URL。

    プロファイルの url とシートの url は形が乖離しがち（@handle / /channel/ID / 動画URL）で
    URL 主キーにすると突合が壊れ無駄な再分析を招くため、安定する channel_name を主キーにする。
    同名異チャンネルの衝突は _bm_upsert 側で url 曖昧性解消して両保持する。"""
    n = (channel_name or "").strip().lower()
    if n:
        return "name:" + n
    u = _bm_norm_url(url)
    return ("url:" + u) if u else ""


def _bm_upsert(by_key: dict, p: dict) -> None:
    """既存 dict へプロファイルを channel_name 主キーで upsert（同名は後勝ち更新）。

    注意: プロファイルの url とシートの url は同一チャンネルでも形が乖離する（@handle /
    /channel/ID / 動画URL）ため、url で別チャンネル判定はできない（共通ケースで重複が出る）。
    よって name 主キーの単純 upsert とし、同一表示名の別チャンネル共存は既知の軽微制約とする。"""
    k = _bm_profile_key(p.get("channel_name"), p.get("url"))
    if k:
        by_key[k] = p


def run_full_benchmark(sheet_a_url: str = "", sheet_b_url: str = "",
                      extra_urls: list = None, cli_cmd: str = DEFAULT_CLI,
                      progress_cb=None, channel_filter: list = None,
                      skip_existing: bool = True, force: bool = False,
                      max_age_days: int = None, dry_run: bool = False) -> dict:
    """ベンチマーク完成パイプライン：
    1. Sheet A + Sheet B から既存チャンネル情報を取得
    2. extra_urls のリスト外チャンネルを YouTube API で追加取得
    3. 各チャンネルの TOP3 動画のコメントを API key で取得
    4. Claude CLI で統合プロファイル生成
    5. benchmark_profiles.json に保存（マージ＝既存を消さない）

    skip_existing: 既存の有効プロファイルがあれば再生成しない（既定True）。
    force: True で既存を無視して全対象を強制再生成。
    max_age_days: スキップの鮮度しきい値。指定時、これより古い分析は再生成（None=無条件スキップ）。
    dry_run: 生成せず {total,new_count,reanalyze_count,skip_count,est_seconds,cli_count} だけ返す。
    """
    extra_urls = [u.strip() for u in (extra_urls or []) if u.strip()]

    def log(msg):
        print(msg)
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    api_key = _resolve_youtube_api_key()

    all_channels = []
    # 1) Sheet A
    if sheet_a_url:
        log("📊 Sheet A を取得中...")
        try:
            sa = import_benchmark_from_sheet(sheet_a_url)
            for tab in sa.get("tabs", []):
                chs = _parse_sheet_a_rows(tab.get("rows", []))
                if chs:
                    log(f"  ✓ タブ '{tab.get('name')}' から {len(chs)} チャンネル")
                    all_channels.extend(chs)
                    break  # 最初の意味のあるタブを採用
        except Exception as e:
            log(f"  ⚠️ Sheet A 取得失敗: {e}")

    # 2) Sheet B (成長トラッキング — 既存チャンネルに merge)
    growth_map = {}
    if sheet_b_url:
        log("📈 Sheet B を取得中...")
        try:
            sb = import_benchmark_from_sheet(sheet_b_url)
            for tab in sb.get("tabs", []):
                m = _parse_sheet_b_rows(tab.get("rows", []))
                if m:
                    growth_map.update(m)
            log(f"  ✓ 成長データ {len(growth_map)} チャンネル")
        except Exception as e:
            log(f"  ⚠️ Sheet B 取得失敗: {e}")

    # merge growth
    for ch in all_channels:
        g = growth_map.get(ch["channel_name"])
        if g:
            ch["growth"] = g

    # 3) extra_urls（リスト外チャンネル）
    for url in extra_urls:
        log(f"➕ 追加チャンネル: {url}")
        try:
            meta = fetch_channel_basic_by_key(url, api_key)
            meta["source"] = "extra_url"
            all_channels.append(meta)
            log(f"  ✓ {meta.get('channel_name')}")
        except Exception as e:
            log(f"  ❌ 失敗: {e}")

    # フィルタ適用（指定があれば選択チャンネルだけ残す）
    if channel_filter:
        wanted = set(n.strip() for n in channel_filter if n and n.strip())
        before = len(all_channels)
        all_channels = [ch for ch in all_channels if (ch.get("channel_name") or "").strip() in wanted]
        log(f"🎯 フィルタ適用: {before} → {len(all_channels)} チャンネル")

    # 既存プロファイルをロード（S0 マージ保存 / S1 スキップ判定の基盤）
    import datetime as _dt
    existing_payload = {}
    existing_by_key = {}
    existing_unkeyed = []  # name/url とも空でキー化できない既存プロファイル（消さず carry）
    if BENCHMARK_PROFILES_FILE.exists():
        try:
            existing_payload = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
            for p in (existing_payload.get("profiles") or []):
                if _bm_profile_key(p.get("channel_name"), p.get("url")):
                    _bm_upsert(existing_by_key, p)
                else:
                    existing_unkeyed.append(p)
        except Exception:
            existing_payload, existing_by_key, existing_unkeyed = {}, {}, []

    def _is_valid(p):
        prof = (p or {}).get("profile")
        return isinstance(prof, dict) and not prof.get("error")

    def _is_fresh(p):
        if max_age_days is None:
            return True
        ga = (p or {}).get("_analyzed_at") or existing_payload.get("generated_at")
        if not ga:
            return False
        try:
            return (_dt.datetime.now() - _dt.datetime.fromisoformat(ga)).days < max_age_days
        except Exception:
            return False

    # スキップ判定（S1）: force でなく skip_existing かつ 既存が有効＆鮮度内なら再生成しない。
    # error 持ち / 古い / 未分析 は生成対象（失敗のみ自動再試行を含む）。
    plan = []  # [(ch, action, existing_profile)]  action ∈ {"generate","reuse"}
    for ch in all_channels:
        k = _bm_profile_key(ch.get("channel_name"), ch.get("url"))
        ex = existing_by_key.get(k)
        if (not force) and skip_existing and ex is not None and _is_valid(ex) and _is_fresh(ex):
            plan.append((ch, "reuse", ex))
        else:
            plan.append((ch, "generate", ex))

    gen_new = sum(1 for _, a, ex in plan if a == "generate" and ex is None)
    gen_re = sum(1 for _, a, ex in plan if a == "generate" and ex is not None)
    reuse_n = sum(1 for _, a, _ in plan if a == "reuse")
    gen_total = gen_new + gen_re

    # dry_run（S3 コスト見積り）: 生成・refresh・comment を一切呼ばず内訳のみ返す
    if dry_run:
        return {
            "dry_run": True,
            "total": len(all_channels),
            "new_count": gen_new,
            "reanalyze_count": gen_re,
            "skip_count": reuse_n,
            "cli_count": gen_total,
            "est_seconds": gen_total * 50,
        }

    # 4) 生成対象だけ Data API で最新化（reuse は最新化不要＝search.list quota 節約）
    if api_key:
        for i, (ch, action, ex) in enumerate(plan):
            if action != "generate":
                continue
            name = ch.get("channel_name") or ch.get("url") or "(unknown)"
            try:
                log(f"🔄 最新動画情報を Data API で更新: {name}")
                plan[i] = (refresh_channel_with_youtube_api(ch, api_key), action, ex)
            except Exception as e:
                log(f"  ⚠️ 更新失敗（Sheet値で続行）: {e}")
    else:
        log("⚠️ YouTube API key 未設定のため、Sheet の動画情報を使用します")

    # 5) 各チャンネルの TOP3 コメント取得 + プロファイル生成（reuse は流用）
    profiles = []
    total = len(plan)
    log(f"\n🧠 対象 {total} ch（生成 {gen_total}・流用 {reuse_n}）プロファイル処理開始...")
    done = 0
    for idx, (ch, action, ex) in enumerate(plan, 1):
        name = ch.get("channel_name") or "(unknown)"
        if action == "reuse":
            log(f"[{idx}/{total}] ⏭ スキップ(分析済み・流用): {name}")
            profiles.append(ex)
            continue
        done += 1
        log(f"\n[{idx}/{total}] 🔍 {name}（生成 {done}/{gen_total}）")
        comments_by_video = {}
        if api_key:
            for v in (ch.get("top_videos") or [])[:3]:
                vid = v.get("video_id") or _extract_video_id(v.get("url", ""))
                if not vid:
                    continue
                log(f"  💬 コメント取得: {v.get('title', '')[:50]}")
                cs = fetch_video_comments_by_key(vid, api_key, max_results=30)
                comments_by_video[vid] = cs
                log(f"     → {len(cs)} 件")
        else:
            log("  ⚠️ YouTube API key 未設定のためコメントスキップ")

        log("  🧠 Claude で統合プロファイル生成...")
        profile = build_channel_profile(ch, comments_by_video, cli_cmd=cli_cmd)
        profiles.append({
            "channel_name": name,
            "url": ch.get("url", ""),
            "subscribers": ch.get("subscribers", 0),
            "total_views": ch.get("total_views", 0),
            "thumbnail": ch.get("thumbnail", ""),
            "growth": ch.get("growth"),
            "top_videos": ch.get("top_videos", []),
            "recent_videos": ch.get("recent_videos", []),
            "comments_sample": {vid: cs[:3] for vid, cs in comments_by_video.items()},
            "profile": profile,
            "source": ch.get("source", "unknown"),
            "_analyzed_at": _dt.datetime.now().isoformat(),
        })

    # 6) 保存（S0 マージ＝既存全件を保持し、今回対象だけ upsert。選択外を消さない）
    merged = dict(existing_by_key)
    for p in profiles:
        if _bm_profile_key(p.get("channel_name"), p.get("url")):
            _bm_upsert(merged, p)
        else:
            existing_unkeyed.append(p)  # 今回分でキー化不能なものも消さず carry
    final_profiles = list(merged.values()) + existing_unkeyed
    payload = {
        "generated_at": _dt.datetime.now().isoformat(),
        "sheet_a_url": sheet_a_url,
        "sheet_b_url": sheet_b_url,
        "extra_urls": extra_urls,
        "profiles": final_profiles,
    }
    BENCHMARK_PROFILES_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n✅ 保存: {BENCHMARK_PROFILES_FILE}（全{len(final_profiles)} / 今回生成{gen_total}・流用{reuse_n}）")
    return payload
