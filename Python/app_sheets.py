#!/usr/bin/env python3
"""orzz. Google スプレッドシート競合データ取得モジュール
=======================================================

公開 CSV エクスポート URL からチャンネル詳細 + 成長トラッキングを取得し、
既存の competitor_data スキーマに変換する。YouTube API quota ゼロ。

Sheet 1 (チャンネル詳細): 195 チャンネル、TOP5/新着5 動画 + 非表示の再生数/いいね
Sheet 2 (成長トラッキング): 54 チャンネル、日次伸び率（Channel Tracker 自動更新）
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# ─── データクラス ────────────────────────────────────────

@dataclass
class VideoInfo:
    thumbnail: str = ""
    title: str = ""
    url: str = ""
    publish_date: str = ""
    publish_datetime: str = ""
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    video_id: str = ""

    def to_competitor_schema(self, channel_title: str = "") -> dict:
        """既存の fetch_competitor_data() が返す video dict と同じ形式に変換"""
        vid = ""
        m = re.search(r"[?&]v=([\w-]{11})", self.url)
        if m:
            vid = m.group(1)
        return {
            "videoId": vid or self.video_id,
            "title": self.title,
            "description": "",
            "tags": [],
            "publishedAt": self.publish_datetime or self.publish_date,
            "viewCount": self.view_count,
            "likeCount": self.like_count,
            "commentCount": self.comment_count,
            "duration": "",
            "channelTitle": channel_title,
        }


@dataclass
class ChannelDetail:
    url: str = ""
    title: str = ""
    country_code: str = ""
    description: str = ""
    video_count: int = 0
    total_views: int = 0
    subscribers: int = 0
    created_date: str = ""
    memo: str = ""
    icon_url: str = ""  # ICON IMAGE 列から抽出（=IMAGE("...") の URL 部分）
    top_videos: list[VideoInfo] = field(default_factory=list)
    recent_videos: list[VideoInfo] = field(default_factory=list)


@dataclass
class GrowthEntry:
    channel_name: str = ""
    tracking_start: str = ""
    last_updated: str = ""
    total_views: int = 0
    subscribers: int = 0
    daily_view_change: int = 0
    daily_sub_change: int = 0
    growth_rate: float = 0.0
    score: float = 0.0  # 複合スコア（identify_hot_channels で計算）


# ─── CSV 取得 ────────────────────────────────────────────

class SheetFetchError(RuntimeError):
    """スプシ CSV 取得時の代表的な失敗（URL 形式・公開設定・認証）"""


_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_\-]+)")
_GID_RE = re.compile(r"[?&#]gid=(\d+)")


def normalize_sheet_url(url: str) -> str:
    """Google スプレッドシートの URL を CSV エクスポート形式へ正規化する。

    受け付ける形式:
      - https://docs.google.com/spreadsheets/d/{id}/edit?gid=N
      - https://docs.google.com/spreadsheets/d/{id}/edit#gid=N
      - https://docs.google.com/spreadsheets/d/{id}/edit
      - https://docs.google.com/spreadsheets/d/{id}/
      - https://docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&gid=N (そのまま)
      - https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid=N (そのまま)
      - https://docs.google.com/spreadsheets/d/{id}/pub?output=csv (そのまま)

    変換結果: https://docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&gid={N|0}
    URL に sheet ID が見つからない場合は元の URL をそのまま返す（後段でエラー処理）。
    """
    if not url:
        return url
    u = url.strip()
    if not u:
        return u
    # 既に CSV エクスポート形式ならそのまま
    if "/gviz/tq" in u or "/export?format=csv" in u or "/pub?output=csv" in u:
        return u
    m = _SHEET_ID_RE.search(u)
    if not m:
        return u
    sheet_id = m.group(1)
    g = _GID_RE.search(u)
    gid = g.group(1) if g else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"


def fetch_csv(url: str, timeout: int = 30) -> list[list[str]]:
    """Google スプレッドシートの公開 CSV をダウンロードして 2D リストで返す。

    URL は自動的に CSV エクスポート形式へ正規化される。
    HTML が返ってきた場合（URL 形式が間違い / シートが非公開 / Google ログイン要求）は
    SheetFetchError を投げる。CSV 由来の解析結果と HTML 由来の偽データを混ぜないため。
    """
    if not url or not url.strip():
        raise SheetFetchError("スプレッドシート URL が未設定です")
    u = normalize_sheet_url(url.strip())
    if "/gviz/tq" not in u and "/export?format=csv" not in u and "/pub?output=csv" not in u:
        raise SheetFetchError(
            "URL から sheet ID を抽出できませんでした。"
            "Google スプレッドシートの URL を入力してください。"
            f" 受信: {url.strip()[:120]}"
        )
    req = urllib.request.Request(u, headers={"User-Agent": "automation-studio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise SheetFetchError(f"HTTP {e.code}: スプシ取得失敗（公開設定を確認してください）") from e
    except Exception as e:
        raise SheetFetchError(f"取得エラー: {e}") from e
    # HTML が返ってきた場合（ログインページ / 権限エラーページ）
    head = data[:200].decode("utf-8", errors="replace").lstrip().lower()
    if "text/html" in content_type or head.startswith(("<!doctype", "<html", "<head")):
        raise SheetFetchError(
            "CSV ではなく HTML ページが返されました。"
            "シートが「リンクを知っている全員に閲覧可」になっているか確認してください。"
        )
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return list(reader)


def _parse_int(val: str) -> int:
    try:
        return int(val.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


def _parse_float(val: str) -> float:
    try:
        return float(val.replace(",", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _safe_get(row: list, idx: int) -> str:
    if idx < len(row):
        return row[idx].strip()
    return ""


# ─── Sheet 1: チャンネル詳細パーサ ──────────────────────

# 列マッピング（0-indexed）
# TOP 動画: 各 8 列 (サムネ, タイトル, URL, 公開日, 公開日時, 再生数, いいね, コメント)
_TOP_VIDEO_OFFSETS = [
    (13, 14, 15, 16, 17, 18, 19, 20),  # TOP 1
    (21, 22, 23, 24, 25, 26, 27, 28),  # TOP 2
    (29, 30, 31, 32, 33, 34, 35, 36),  # TOP 3
    (37, 38, 39, 40, 41, 42, 43, 44),  # TOP 4
    (45, 46, 47, 48, 49, 50, 51, 52),  # TOP 5
]

# 新着動画: 各 7 列 (サムネ, タイトル, URL, 公開日, 公開日時, 再生数, いいね) ※コメントなし
_RECENT_VIDEO_OFFSETS = [
    (54, 55, 56, 57, 58, 59, 60),  # 新着 1
    (61, 62, 63, 64, 65, 66, 67),  # 新着 2
    (68, 69, 70, 71, 72, 73, 74),  # 新着 3
    (75, 76, 77, 78, 79, 80, 81),  # 新着 4
    (82, 83, 84, 85, 86, 87, 88),  # 新着 5
]


def _parse_video_top(row: list, offsets: tuple) -> VideoInfo | None:
    thumb_i, title_i, url_i, date_i, dt_i, views_i, likes_i, comments_i = offsets
    title = _safe_get(row, title_i)
    if not title:
        return None
    return VideoInfo(
        thumbnail=_safe_get(row, thumb_i),
        title=title,
        url=_safe_get(row, url_i),
        publish_date=_safe_get(row, date_i),
        publish_datetime=_safe_get(row, dt_i),
        view_count=_parse_int(_safe_get(row, views_i)),
        like_count=_parse_int(_safe_get(row, likes_i)),
        comment_count=_parse_int(_safe_get(row, comments_i)),
    )


def _parse_video_recent(row: list, offsets: tuple) -> VideoInfo | None:
    thumb_i, title_i, url_i, date_i, dt_i, views_i, likes_i = offsets
    title = _safe_get(row, title_i)
    if not title:
        return None
    return VideoInfo(
        thumbnail=_safe_get(row, thumb_i),
        title=title,
        url=_safe_get(row, url_i),
        publish_date=_safe_get(row, date_i),
        publish_datetime=_safe_get(row, dt_i),
        view_count=_parse_int(_safe_get(row, views_i)),
        like_count=_parse_int(_safe_get(row, likes_i)),
        comment_count=0,
    )


def _detect_column_index(header_row: list[str], candidates: list[str]) -> int:
    """ヘッダ行から指定名（部分一致・大小無視）の列 index を探す。見つからなければ -1。"""
    norm = lambda s: (s or "").strip().lower().replace(" ", "").replace("_", "")
    targets = [norm(c) for c in candidates]
    for i, cell in enumerate(header_row):
        n = norm(cell)
        if any(t in n for t in targets if t):
            return i
    return -1


_IMAGE_FORMULA_RE = re.compile(r'=?\s*IMAGE\(\s*"([^"]+)"', re.IGNORECASE)
_HYPERLINK_FORMULA_RE = re.compile(
    r'=?\s*HYPERLINK\(\s*"([^"]+)"\s*,\s*IMAGE\(\s*"([^"]+)"', re.IGNORECASE,
)


def _extract_image_url(cell: str) -> str:
    """セル値から画像 URL を抽出。`=IMAGE("...")` 形式 / 生 URL のどちらにも対応。"""
    if not cell:
        return ""
    s = cell.strip()
    m = _IMAGE_FORMULA_RE.search(s)
    if m:
        return m.group(1).strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    return ""


def _extract_hyperlinked_image(cell: str) -> tuple[str, str]:
    """`=HYPERLINK("page_url", IMAGE("img_url"))` を (page_url, img_url) で返す。
    HYPERLINK の代わりに =IMAGE() / 生 URL なら (image_url, image_url) を返す。
    """
    if not cell:
        return ("", "")
    s = cell.strip()
    m = _HYPERLINK_FORMULA_RE.search(s)
    if m:
        return (m.group(1).strip(), m.group(2).strip())
    img = _extract_image_url(s)
    return (img, img)


def parse_channel_detail(rows: list[list[str]]) -> list[ChannelDetail]:
    """Sheet 1 の全行をパース。ヘッダー 2 行をスキップ。
    ICON IMAGE 列があれば自動で検出して channel.icon_url に格納。
    """
    if len(rows) < 3:
        return []
    # ヘッダ行から ICON IMAGE 列の位置を動的検出（任意位置に配置可能）
    header_row = rows[0] or []
    icon_col = _detect_column_index(header_row, ["icon image", "iconimage", "iconurl", "icon_url", "アイコン画像"])
    channels = []
    for row in rows[2:]:  # skip header row + label row
        title = _safe_get(row, 3)
        if not title:
            continue
        icon_url = ""
        if icon_col >= 0:
            icon_url = _extract_image_url(_safe_get(row, icon_col))
        ch = ChannelDetail(
            url=_safe_get(row, 1),
            title=title,
            country_code=_safe_get(row, 4),
            description=_safe_get(row, 6),
            video_count=_parse_int(_safe_get(row, 7)),
            total_views=_parse_int(_safe_get(row, 8)),
            subscribers=_parse_int(_safe_get(row, 9)),
            created_date=_safe_get(row, 10),
            memo=_safe_get(row, 11),
            icon_url=icon_url,
        )
        # TOP 動画
        for offsets in _TOP_VIDEO_OFFSETS:
            v = _parse_video_top(row, offsets)
            if v:
                ch.top_videos.append(v)
        # 新着動画
        for offsets in _RECENT_VIDEO_OFFSETS:
            v = _parse_video_recent(row, offsets)
            if v:
                ch.recent_videos.append(v)
        channels.append(ch)
    return channels


# ─── Sheet 2: 成長トラッキングパーサ ───────────────────

def parse_growth_tracking(rows: list[list[str]]) -> list[GrowthEntry]:
    """Sheet 2 の全行をパース。ヘッダー 1 行をスキップ。"""
    entries = []
    for row in rows[1:]:  # skip header
        name = _safe_get(row, 1)
        if not name:
            continue
        entries.append(GrowthEntry(
            channel_name=name,
            tracking_start=_safe_get(row, 3),
            last_updated=_safe_get(row, 4),
            total_views=_parse_int(_safe_get(row, 5)),
            subscribers=_parse_int(_safe_get(row, 6)),
            daily_view_change=_parse_int(_safe_get(row, 7)),
            daily_sub_change=_parse_int(_safe_get(row, 8)),
            growth_rate=_parse_float(_safe_get(row, 9)),
        ))
    return entries


# ─── ホットチャンネル特定 ─────────────────────────────

def identify_hot_channels(
    entries: list[GrowthEntry],
    top_n: int = 15,
    min_subs: int = 0,
    max_subs: int | None = None,
    exclude_names: list[str] | None = None,
) -> list[GrowthEntry]:
    """伸び率 + 日次再生増 + 日次登録増 の複合スコアでランキング。

    純粋な growth_rate だけだと小規模チャンネルが有利すぎるため、
    絶対増分も加味する。

    フィルタ:
    - min_subs / max_subs: 登録者規模で絞り込み（「小規模高成長」優先運用用）
    - exclude_names: 除外したいチャンネル名（自チャンネル、明らかに無関係など）
    """
    if not entries:
        return []

    excl = set((exclude_names or []))
    filtered = [
        e for e in entries
        if e.subscribers >= min_subs
        and (max_subs is None or e.subscribers <= max_subs)
        and e.channel_name not in excl
    ]
    if not filtered:
        return []

    # 正規化用の最大値
    max_views = max((e.daily_view_change for e in filtered), default=1) or 1
    max_subs_norm = max((e.daily_sub_change for e in filtered), default=1) or 1
    max_rate = max((e.growth_rate for e in filtered), default=1) or 1

    for e in filtered:
        # 複合スコア: 伸び率 40% + 日次再生 40% + 日次登録 20%
        e.score = (
            (e.growth_rate / max_rate) * 0.4
            + (e.daily_view_change / max_views) * 0.4
            + (e.daily_sub_change / max_subs_norm) * 0.2
        )

    ranked = sorted(filtered, key=lambda e: e.score, reverse=True)
    return ranked[:top_n]


# ─── チャンネルマッチング ────────────────────────────

def match_channels(growth_name: str, detail_channels: list[ChannelDetail]) -> ChannelDetail | None:
    """Sheet 2 のチャンネル名を Sheet 1 のチャンネルと照合"""
    # 1) 完全一致
    for ch in detail_channels:
        if ch.title == growth_name:
            return ch
    # 2) 大小無視
    lower = growth_name.lower()
    for ch in detail_channels:
        if ch.title.lower() == lower:
            return ch
    # 3) fuzzy (ratio > 0.8)
    best = None
    best_ratio = 0.0
    for ch in detail_channels:
        ratio = SequenceMatcher(None, lower, ch.title.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = ch
    if best and best_ratio > 0.8:
        return best
    return None


# ─── competitor_data スキーマへの変換 ────────────────

def build_competitor_data(
    enriched: list[tuple[GrowthEntry, ChannelDetail | None]],
) -> dict:
    """(GrowthEntry, ChannelDetail?) のリストを既存スキーマに変換"""
    channels = []
    for growth, detail in enriched:
        if detail:
            top_by_views = [v.to_competitor_schema(detail.title) for v in detail.top_videos]
            recent_uploads = [v.to_competitor_schema(detail.title) for v in detail.recent_videos]
            channels.append({
                "url": detail.url,
                "channelId": "",
                "channelName": detail.title,
                "totalVideos": detail.video_count,
                "subscribers": detail.subscribers,
                "totalViews": detail.total_views,
                "topByViews": top_by_views,
                "recentUploads": recent_uploads,
                "growthRate": growth.growth_rate,
                "dailyViewChange": growth.daily_view_change,
            })
        else:
            # Sheet 2 のみ（詳細データなし）— 成長データだけ入れる
            channels.append({
                "url": "",
                "channelId": "",
                "channelName": growth.channel_name,
                "totalVideos": 0,
                "subscribers": growth.subscribers,
                "totalViews": growth.total_views,
                "topByViews": [],
                "recentUploads": [],
                "growthRate": growth.growth_rate,
                "dailyViewChange": growth.daily_view_change,
            })
    return {"channels": channels}


# ─── 統合エントリポイント ────────────────────────────

def fetch_from_spreadsheets(
    detail_url: str,
    growth_url: str,
    top_n: int = 15,
    pinned_names: list[str] | None = None,
    min_subs: int = 0,
    max_subs: int | None = None,
    exclude_names: list[str] | None = None,
) -> tuple[dict, dict]:
    """Sheet 1 + Sheet 2 からデータ取得 → competitor_data + growth_summary を返す

    pinned_names が指定されていれば、スコア計算は行わずピン留めリストのみを採用。
    未指定ならフィルタ込みで identify_hot_channels() の TOP N を自動選択。
    """
    print(f"  📊 Sheet 2 (成長トラッキング) を取得中...")
    growth_rows = fetch_csv(growth_url)
    growth_entries = parse_growth_tracking(growth_rows)
    print(f"     → {len(growth_entries)} チャンネル")

    if pinned_names:
        name_set = {n.strip() for n in pinned_names if n.strip()}
        hot = [e for e in growth_entries if e.channel_name in name_set]
        # スコア計算（フィルタなし・全エントリ基準の正規化）
        identify_hot_channels(growth_entries, top_n=len(growth_entries))
        hot.sort(key=lambda e: e.score, reverse=True)
        print(f"     → 📌 ピン留め {len(hot)}/{len(pinned_names)} チャンネル採用")
    else:
        hot = identify_hot_channels(
            growth_entries, top_n=top_n,
            min_subs=min_subs, max_subs=max_subs, exclude_names=exclude_names,
        )
        print(f"     → ホット {len(hot)} チャンネル特定 (filter: subs {min_subs}-{max_subs or '∞'})")

    print(f"  📊 Sheet 1 (チャンネル詳細) を取得中...")
    detail_rows = fetch_csv(detail_url)
    detail_channels = parse_channel_detail(detail_rows)
    print(f"     → {len(detail_channels)} チャンネル")

    # マッチング
    enriched = []
    matched = 0
    for entry in hot:
        detail = match_channels(entry.channel_name, detail_channels)
        enriched.append((entry, detail))
        if detail:
            matched += 1
    print(f"     → マッチ成功: {matched}/{len(hot)}")

    competitor_data = build_competitor_data(enriched)

    growth_summary = {
        "hot_channels": [
            {
                "name": e.channel_name,
                "growth_rate": e.growth_rate,
                "daily_views": e.daily_view_change,
                "daily_subs": e.daily_sub_change,
                "total_views": e.total_views,
                "subscribers": e.subscribers,
                "score": round(e.score, 3),
            }
            for e in hot
        ],
        "total_tracked": len(growth_entries),
        "total_detail": len(detail_channels),
    }

    return competitor_data, growth_summary
