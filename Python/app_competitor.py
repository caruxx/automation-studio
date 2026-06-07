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
    from _app_config import (
        resolve_config_dir as _resolve_config_dir,
        resolve_shared_config_dir as _resolve_shared_config_dir,
    )
    CONFIG_DIR = _resolve_config_dir()
    SHARED_CONFIG_DIR = _resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR
CLIENT_SECRET = CONFIG_DIR / "youtube_client_secret.json"
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"
# ベンチマーク先の分析データ/プロファイル/設定は PC 間共有（共有ドライブ側）
CACHE_FILE = SHARED_CONFIG_DIR / "competitor_analysis_cache.json"
CACHE_FILENAME = "competitor_analysis_cache.json"
BENCHMARK_CONFIG_FILE = SHARED_CONFIG_DIR / "benchmark_config.json"
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

# （app_image_prompt の import は D8 で propose_flow_prompt を撤去した際に孤立した死蔵 import。
#  D6 実体移動で competitor を取得層に純化するにあたり除去。画像プロンプト生成は
#  app_image_prompt / app_benchmark_analyze 側で完結し、取得層からは参照しない。）


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

    detail_url = (
        channel_cfg.get("spreadsheet_channel_detail_url", "")
        if "spreadsheet_channel_detail_url" in channel_cfg
        else (dashboard.get("spreadsheet_channel_detail_url") or benchmark.get("spreadsheet_channel_detail_url", ""))
    )
    growth_url = (
        channel_cfg.get("spreadsheet_growth_tracking_url", "")
        if "spreadsheet_growth_tracking_url" in channel_cfg
        else (dashboard.get("spreadsheet_growth_tracking_url") or benchmark.get("spreadsheet_growth_tracking_url", ""))
    )
    bench_filter = (
        channel_cfg.get("benchmark_filter", {})
        if "benchmark_filter" in channel_cfg
        else (dashboard.get("benchmark_filter") or benchmark.get("filter") or {})
    )
    pinned_names = (
        channel_cfg.get("benchmark_pinned_names", [])
        if "benchmark_pinned_names" in channel_cfg
        else (dashboard.get("benchmark_pinned_names") or benchmark.get("pinned_names") or [])
    )

    cfg = dict(dashboard)
    cfg.update({
        "spreadsheet_channel_detail_url": detail_url,
        "spreadsheet_growth_tracking_url": growth_url,
        "benchmark_filter": bench_filter,
        "benchmark_pinned_names": pinned_names,
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

# JSON 抽出は app_benchmark_common に集約（D10）
from app_benchmark_common import extract_json_object as _extract_json_object


# ─── 分析・提案層は app_benchmark_analyze.py へ物理移動（D6 実体移動）───
# analyze_with_claude / propose_with_analysis / propose_suno_prompt /
# analyze_thumbnail_elements は app_benchmark_analyze に定義。
# competitor は YouTube API 取得＋スプシ取込の取得層に純化。
# 内部利用（run_full_analysis / __main__）は app_benchmark_analyze から局所 import する。


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
    # 分析関数は app_benchmark_analyze へ物理移動済み（D6）。局所 import で逆依存を持たない。
    from app_benchmark_analyze import analyze_with_claude
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
    try:
        from app_channel_cache import save_scoped_cache
        cache = save_scoped_cache(CACHE_FILENAME, CACHE_FILE, cache)
    except Exception:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 キャッシュ保存: {CACHE_FILE} (source: {source}, language: ja, v5)")
    return cache


def load_cache() -> dict | None:
    try:
        from app_channel_cache import load_scoped_cache
        d = load_scoped_cache(CACHE_FILENAME, CACHE_FILE, None)
        return d if isinstance(d, dict) else None
    except Exception:
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

            from app_benchmark_analyze import propose_with_analysis
            result = propose_with_analysis(
                cache["analysis"], cache["competitor_data"],
                cli_cmd=args.cli, current_title=current_title,
                songs=songs, persona=cfg.get("persona", ""),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


# ─── ベンチマーク完成パイプライン（API key ベース・全自動） ──────────

BENCHMARK_PROFILES_FILE = SHARED_CONFIG_DIR / "benchmark_profiles.json"


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

    # Claude→Codex 共通ランナー。両方失敗時は {"error":...} で握る（プロファイル単位の劣化）
    from app_llm_runner import run_llm
    try:
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="channel-profile").strip()
        parsed = _extract_json_object(out)
        if not parsed:
            return {"error": "JSON解析失敗", "raw": out[:500]}
        return parsed
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
