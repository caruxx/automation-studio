#!/usr/bin/env python3
"""orzz. Google スプレッドシート競合データ取得モジュール
=======================================================

公開 CSV エクスポート URL からチャンネル詳細 + 成長トラッキングを取得し、
既存の competitor_data スキーマに変換する。YouTube API quota ゼロ。

Sheet 1 (チャンネル詳細・リサーチシート): TOP5/新着5 動画 + 再生数/いいね。
  列はヘッダ名で動的検出（英語技術名 URL/TITLE/... と日本語ラベル 取得対象URL/チャンネル名/... 両対応）。
  ※旧 195ch の全量データは同 book の BACKUP タブに退避されている（現行タブは絞り込み済み）。
Sheet 2 (成長トラッキング): CHANNEL_TRACK 15列、日次伸び率（Channel Tracker 自動更新）
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    # CHANNEL_TRACK 15列マスタの追加列（列10-14）
    video_count: int = 0          # 動画本数
    last_post_date: str = ""      # 直近投稿日
    recent5_avg_views: int = 0    # 直近5本平均再生
    weekly_growth_rate: float = 0.0   # 週次伸び率
    monthly_growth_rate: float = 0.0  # 月次伸び率
    icon_url: str = ""            # アイコン列（=IMAGE）から抽出
    score: float = 0.0  # 複合スコア（identify_hot_channels で計算）
    is_new: bool = False  # 新着チャンネル判定（detect_new_channels で付与）
    days_tracked: int = -1  # 追跡開始日からの経過日数（detect_new_channels で付与）


@dataclass
class VideoEvent:
    """個別タブで検出した「新着動画」イベント（タイトル変化点）"""
    date: str = ""          # "M/D"（タイトルが最初に現れた日）
    title: str = ""
    first_v48: int = 0      # その動画が最新だった間の 48h 再生ピーク = 初速
    peak_views: int = 0     # 動画別再生数の最大
    ctr: float = 0.0        # クリック率（手動入力列・空が多い）
    likes_rate: float = 0.0  # いいね率


@dataclass
class ChannelTimeline:
    """個別 TRACK_ タブ（1日4回取得の時系列）から算出した指標一式"""
    channel_name: str = ""
    gid: int = 0
    days: int = 0
    rows_parsed: int = 0
    latest_subs: int = 0
    latest_total_views: int = 0
    sub_gain_7d: int = 0
    sub_gain_prev7d: int = 0
    accel: float = 0.0        # sub_gain_7d / prev（前7日が0で直近>0 なら -1=新規急加速）
    accel_label: str = ""     # 加速 / 横ばい / 減速 / 新規急加速 / データ不足
    v48_peak_recent: int = 0  # 直近の 48h 再生ピーク（刺さりシグナル）
    daily_subs: list = field(default_factory=list)   # [(date_str, subs)]（UIグラフ用）
    daily_views: list = field(default_factory=list)  # [(date_str, total_views)]
    new_videos: list = field(default_factory=list)   # list[VideoEvent]（新しい順）
    last_data_date: str = ""
    fresh_days_ago: int = -1  # 最終取得日が何日前か（-1=不明）
    is_stale: bool = False    # データ陳腐化（しきい値超過）
    error: str = ""


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

# 旧形式の固定列マッピング（0-indexed）。ヘッダ検出に失敗した場合のフォールバック専用。
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


_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")


def _extract_video_id(url: str) -> str:
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _norm_header(s: str) -> str:
    """ヘッダ名の正規化: 改行/空白/アンダースコア除去 + 小文字化（完全一致判定用）。"""
    return (s or "").replace("\n", "").replace(" ", "").replace("_", "").strip().lower()


# チャンネル列のヘッダ候補（正規化後の完全一致）。
# リサーチシートは 1 行目=英語技術名（URL/ICON_IMAGE/TITLE/...）、2 行目=日本語ラベル。
# gviz CSV 経路は英語ヘッダ行が落ちて日本語ラベル行が先頭になるため両対応にする。
_DETAIL_CHANNEL_COLS: dict[str, list[str]] = {
    "url": ["url", "取得対象url"],
    "icon": ["iconimage", "chアイコン", "iconurl", "アイコン画像"],
    "title": ["title", "チャンネル名"],
    "country": ["countrycode", "地域"],
    "description": ["description", "チャンネル説明"],
    "video_count": ["videocount", "総動画数"],
    "total_views": ["totalviews", "総再生回数"],
    "subscribers": ["subscribers", "登録者数"],
    "created": ["publisheddate", "開設日"],
    "memo": ["memo", "メモ"],
}


def _top_video_col_candidates(n: int) -> dict[str, list[str]]:
    """TOP n 位（1-5）の各列ヘッダ候補。英語は 1 位のみ無印（TOP_VIDEO_TITLE）、2 位以降は数字付き。"""
    sfx = "" if n == 1 else str(n)
    return {
        "thumb": [f"topvideothumbnail{sfx}", f"{n}位サムネイル"],
        "title": [f"topvideotitle{sfx}", f"{n}位タイトル"],
        "url": [f"topvideourl{sfx}", f"{n}位url"],
        "date": [f"topvideodate{sfx}", f"{n}位公開日"],
        "datetime": [f"topvideodatetime{sfx}", f"{n}位公開時間"],
        "views": [f"topvideoviews{sfx}", f"{n}位再生回数"],
        "likes": [f"topvideolikes{sfx}", f"{n}位高評価数"],
        "comments": [f"topvideocomments{sfx}", f"{n}位コメント数"],
    }


def _recent_video_col_candidates(n: int) -> dict[str, list[str]]:
    """新着 n（1-5）の各列ヘッダ候補（コメント列なし）。"""
    return {
        "thumb": [f"videothumbnail{n}", f"新着{n}サムネイル"],
        "title": [f"videotitle{n}", f"新着{n}タイトル"],
        "url": [f"videourl{n}", f"新着{n}url"],
        "date": [f"videodate{n}", f"新着{n}公開日"],
        "datetime": [f"videodatetime{n}", f"新着{n}公開時間"],
        "views": [f"videoviews{n}", f"新着{n}再生回数"],
        "likes": [f"videolikes{n}", f"新着{n}高評価数"],
    }


def _map_cols(norm_cells: list[str], candidates: dict[str, list[str]]) -> dict[str, int]:
    """正規化済みヘッダセル列から {field: col_index} を構築。見つからない field は -1。"""
    out = {}
    for field, cands in candidates.items():
        out[field] = -1
        for i, cell in enumerate(norm_cells):
            if cell in cands:
                out[field] = i
                break
    return out


def _find_detail_header(rows: list[list[str]]) -> tuple[int, dict[str, int], list[str]]:
    """先頭 5 行からヘッダ行を探索。(行 index, チャンネル列マップ, 正規化セル列) を返す。
    title + subscribers が見つかった行をヘッダとみなす。失敗時は (-1, {}, [])。
    """
    for ri, row in enumerate(rows[:5]):
        norm_cells = [_norm_header(c) for c in (row or [])]
        idx = _map_cols(norm_cells, _DETAIL_CHANNEL_COLS)
        if idx.get("title", -1) >= 0 and idx.get("subscribers", -1) >= 0:
            return ri, idx, norm_cells
    return -1, {}, []


def _video_from_cols(row: list, cols: dict[str, int], with_comments: bool) -> VideoInfo | None:
    def g(field: str) -> str:
        i = cols.get(field, -1)
        return _safe_get(row, i) if i >= 0 else ""

    title = g("title")
    url = g("url")
    if not title and not url:
        return None
    vid = _extract_video_id(url)
    # サムネセルは =IMAGE() 数式のため CSV では空になりがち → video_id から合成
    thumb = _extract_hyperlinked_image(g("thumb"))[1]
    if not thumb and vid:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return VideoInfo(
        thumbnail=thumb,
        title=title,
        url=url,
        publish_date=g("date"),
        publish_datetime=g("datetime"),
        view_count=_parse_int(g("views")),
        like_count=_parse_int(g("likes")),
        comment_count=_parse_int(g("comments")) if with_comments else 0,
        video_id=vid,
    )


def parse_channel_detail(rows: list[list[str]]) -> list[ChannelDetail]:
    """Sheet 1（リサーチシート）の全行をパース。

    列はヘッダ名で動的検出（parse_channel_timeline 方式・英語技術名/日本語ラベル両対応）。
    gviz CSV は英語ヘッダ行が落ち日本語ラベル行が先頭、export CSV は英語+日本語の 2 行が
    残るため、先頭 5 行からヘッダ行を探索し、データ行に混ざるラベル行はスキップする。
    ヘッダが見つからない場合は旧形式の固定列マッピングにフォールバック。
    """
    if not rows:
        return []
    hi, idx, norm_cells = _find_detail_header(rows)
    if hi < 0:
        return _parse_channel_detail_legacy(rows)

    top_groups = [_map_cols(norm_cells, _top_video_col_candidates(n)) for n in range(1, 6)]
    recent_groups = [_map_cols(norm_cells, _recent_video_col_candidates(n)) for n in range(1, 6)]
    title_labels = set(_DETAIL_CHANNEL_COLS["title"])

    def g(row: list, field: str) -> str:
        i = idx.get(field, -1)
        return _safe_get(row, i) if i >= 0 else ""

    channels = []
    for row in rows[hi + 1:]:
        title = g(row, "title")
        if not title:
            continue
        if _norm_header(title) in title_labels:
            continue  # 英語ヘッダ直後の日本語ラベル行など
        ch = ChannelDetail(
            url=g(row, "url"),
            title=title,
            country_code=g(row, "country"),
            description=g(row, "description"),
            video_count=_parse_int(g(row, "video_count")),
            total_views=_parse_int(g(row, "total_views")),
            subscribers=_parse_int(g(row, "subscribers")),
            created_date=g(row, "created"),
            memo=g(row, "memo"),
            icon_url=_extract_image_url(g(row, "icon")),
        )
        for cols in top_groups:
            v = _video_from_cols(row, cols, with_comments=True)
            if v:
                ch.top_videos.append(v)
        for cols in recent_groups:
            v = _video_from_cols(row, cols, with_comments=False)
            if v:
                ch.recent_videos.append(v)
        channels.append(ch)
    return channels


def _parse_channel_detail_legacy(rows: list[list[str]]) -> list[ChannelDetail]:
    """旧形式（固定列・ヘッダ 2 行スキップ）のフォールバックパーサ。"""
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
    """CHANNEL_TRACK マスタ（15列）の全行をパース。ヘッダー 1 行をスキップ。

    列マッピング（0-indexed）:
      0 アイコン / 1 チャンネル名 / 2 個別シート / 3 追跡開始日 / 4 取得日時 /
      5 総再生回数 / 6 登録者数 / 7 前日比再生数 / 8 前日比登録者数 / 9 直近伸び率 /
      10 動画本数 / 11 直近投稿日 / 12 直近5本平均再生 / 13 週次伸び率 / 14 月次伸び率
    旧10列スプシでも _safe_get が空文字を返すため後方互換（列10-14 は 0/空 になる）。
    """
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
            video_count=_parse_int(_safe_get(row, 10)),
            last_post_date=_safe_get(row, 11),
            recent5_avg_views=_parse_int(_safe_get(row, 12)),
            weekly_growth_rate=_parse_float(_safe_get(row, 13)),
            monthly_growth_rate=_parse_float(_safe_get(row, 14)),
            icon_url=_extract_image_url(_safe_get(row, 0)),
        ))
    return entries


# ─── 個別 TRACK_ タブ（時系列）: gid 解決 + 時系列パーサ ──────────

_CONFIG_DIR = os.path.expanduser("~/.config/orzz")
_GID_CACHE_PATH = os.path.join(_CONFIG_DIR, "track_gid_map.json")
_TRACK_PREFIX = "TRACK_"
_STALE_DAYS = 3  # 最終取得日がこれより前なら陳腐化（ホット判定から除外する目安）


def _extract_sheet_id(url_or_id: str) -> str:
    """URL または生 ID から spreadsheet ID を取り出す。"""
    m = _SHEET_ID_RE.search(url_or_id or "")
    if m:
        return m.group(1)
    s = (url_or_id or "").strip()
    return s if re.fullmatch(r"[A-Za-z0-9_\-]+", s) else ""


def _js_unescape(s: str) -> str:
    """htmlview の bootstrap JS 文字列をデコード（\\x26 → & など）。"""
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")


def fetch_tab_gid_map(spreadsheet_url_or_id: str, timeout: int = 25) -> dict[str, int]:
    """htmlview からタブ名 → gid の全マップを抽出（非表示タブ・TRACK_ タブ含む）。"""
    sid = _extract_sheet_id(spreadsheet_url_or_id)
    if not sid:
        raise SheetFetchError("spreadsheet ID を解決できませんでした")
    u = f"https://docs.google.com/spreadsheets/d/{sid}/htmlview"
    req = urllib.request.Request(u, headers={"User-Agent": "automation-studio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_txt = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SheetFetchError(f"HTTP {e.code}: htmlview 取得失敗（公開設定を確認）") from e
    except Exception as e:
        raise SheetFetchError(f"htmlview 取得エラー: {e}") from e
    out: dict[str, int] = {}
    for m in re.finditer(r'name:\s*"((?:[^"\\]|\\.)*?)"[^}]*?(\d{6,})', html_txt):
        name = _js_unescape(m.group(1))
        gid = int(m.group(2))
        out.setdefault(name, gid)
    if not out:
        raise SheetFetchError("htmlview からタブを 1 件も抽出できませんでした")
    return out


def _load_gid_cache() -> dict:
    try:
        with open(_GID_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_gid_cache(cache: dict) -> None:
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        tmp = _GID_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _GID_CACHE_PATH)
    except Exception:
        pass  # キャッシュ保存失敗は致命ではない（毎回 htmlview を引くだけ）


def resolve_track_gid(
    spreadsheet_url_or_id: str, channel_name: str, refresh: bool = False
) -> int | None:
    """チャンネル名 → 個別タブ `TRACK_<名前>` の gid を解決（キャッシュ付き）。

    キャッシュミス時は htmlview を 1 回だけ引いて book 全体を更新・保存する。
    """
    sid = _extract_sheet_id(spreadsheet_url_or_id)
    if not sid:
        return None
    tab = _TRACK_PREFIX + (channel_name or "").strip()
    cache = _load_gid_cache()
    book = cache.get(sid) or {}
    if not refresh and tab in book:
        return int(book[tab])
    try:
        fresh = fetch_tab_gid_map(sid)
    except SheetFetchError:
        return book.get(tab)  # 取得失敗時は既存キャッシュにフォールバック
    cache[sid] = fresh
    _save_gid_cache(cache)
    val = fresh.get(tab)
    return int(val) if val is not None else None


def _infer_date(month: int, day: int, today: date) -> date | None:
    """月日（年なし）を「今日以前で最も近い実日付」に推定（年跨ぎ対応）。"""
    for y in (today.year, today.year - 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if d <= today:
            return d
    return None


def parse_channel_timeline(
    rows: list[list[str]], channel_name: str = "", today: date | None = None
) -> ChannelTimeline:
    """個別 TRACK_ タブ（1日4回取得の時系列）をパースして指標を算出。

    ヘッダ例: 月/日/曜日/取得時間/登録者/登録者前日比/総再生数/前日比再生数/48h再生数/
              .../サムネイル/タイトル/再生数/クリック率/いいね率/コメント数/再生時間/メモ
    列はヘッダ名で動的検出（位置変化に強くする）。行は時系列順（古→新）前提。
    """
    tl = ChannelTimeline(channel_name=channel_name)
    if today is None:
        today = date.today()
    if not rows or len(rows) < 2:
        tl.error = "データなし"
        return tl
    hdr = rows[0]

    def ci(*labels: str) -> int:
        for i, h in enumerate(hdr):
            hn = (h or "").replace("\n", "").strip()
            if hn in labels:
                return i
        return -1

    iM, iD, iT = ci("月"), ci("日"), ci("取得時間")
    iSub, iTV, iV48 = ci("登録者"), ci("総再生数"), ci("48h再生数")
    iTitle, iCtr, iVws = ci("タイトル"), ci("クリック率"), ci("再生数")
    iLikes = ci("いいね率")
    if iM < 0 or iD < 0 or iSub < 0:
        tl.error = "想定ヘッダ（月/日/登録者）が見つかりません"
        return tl

    def num(s):
        s = (s or "").replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    # 1) 生レコード列（時系列順）
    records = []
    for r in rows[1:]:
        if len(r) <= iSub:
            continue
        mo, dd, sub = num(_safe_get(r, iM)), num(_safe_get(r, iD)), num(_safe_get(r, iSub))
        if mo is None or dd is None or sub is None:
            continue
        records.append({
            "key": (int(mo), int(dd)),
            "sub": int(sub),
            "tv": int(num(_safe_get(r, iTV)) or 0) if iTV >= 0 else 0,
            "v48": int(num(_safe_get(r, iV48)) or 0) if iV48 >= 0 else 0,
            "title": _safe_get(r, iTitle) if iTitle >= 0 else "",
            "ctr": num(_safe_get(r, iCtr)) if iCtr >= 0 else None,
            "vws": int(num(_safe_get(r, iVws)) or 0) if iVws >= 0 else 0,
            "likes": num(_safe_get(r, iLikes)) if iLikes >= 0 else None,
        })
    tl.rows_parsed = len(records)
    if not records:
        tl.error = "有効な時系列行がありません"
        return tl

    # 2) 日次系列（各日の最終取得を採用 / 挿入順=時系列順を保持）
    daily: dict = {}
    for rec in records:
        daily[rec["key"]] = rec
    series = list(daily.values())
    tl.days = len(series)
    latest = series[-1]
    tl.latest_subs = latest["sub"]
    tl.latest_total_views = latest["tv"]
    tl.daily_subs = [(f"{k[0]}/{k[1]}", v["sub"]) for k, v in daily.items()]
    tl.daily_views = [(f"{k[0]}/{k[1]}", v["tv"]) for k, v in daily.items()]

    # 3) 登録者の伸び加速度（直近7日 vs その前7日 の純増）
    def gain(seg):
        return seg[-1]["sub"] - seg[0]["sub"] if len(seg) >= 2 else 0

    last7 = series[-8:]
    prev7 = series[-15:-7]
    tl.sub_gain_7d = gain(last7)
    tl.sub_gain_prev7d = gain(prev7)
    if len(series) < 4:
        tl.accel, tl.accel_label = 0.0, "データ不足"
    elif tl.sub_gain_prev7d > 0:
        tl.accel = round(tl.sub_gain_7d / tl.sub_gain_prev7d, 2)
        tl.accel_label = "加速" if tl.accel > 1.3 else ("減速" if tl.accel < 0.7 else "横ばい")
    elif tl.sub_gain_7d > 0:
        tl.accel, tl.accel_label = -1.0, "新規急加速"
    else:
        tl.accel, tl.accel_label = 0.0, "横ばい"

    # 4) 48h 初速ピーク（直近 ~12 日相当 = 末尾48レコード）
    tail = records[-48:]
    tl.v48_peak_recent = max((rec["v48"] for rec in tail), default=0)

    # 5) 新着動画イベント（タイトル変化点でグルーピング・新しい順）
    events = []
    cur = None
    for rec in records:
        t = (rec["title"] or "").strip()
        if not t:
            continue
        if cur is None or t != cur["title"]:
            cur = {"date": f'{rec["key"][0]}/{rec["key"][1]}', "title": t,
                   "v48": rec["v48"], "vws": rec["vws"],
                   "ctr": rec["ctr"], "likes": rec["likes"]}
            events.append(cur)
        else:
            cur["v48"] = max(cur["v48"], rec["v48"])
            cur["vws"] = max(cur["vws"], rec["vws"])
            if cur["ctr"] is None and rec["ctr"] is not None:
                cur["ctr"] = rec["ctr"]
            if cur["likes"] is None and rec["likes"] is not None:
                cur["likes"] = rec["likes"]
    tl.new_videos = [
        VideoEvent(date=e["date"], title=e["title"], first_v48=e["v48"],
                   peak_views=e["vws"], ctr=e["ctr"] or 0.0, likes_rate=e["likes"] or 0.0)
        for e in reversed(events[-8:])
    ]

    # 6) 鮮度（最終取得日 vs 今日）
    lk = latest["key"]
    tl.last_data_date = f"{lk[0]}/{lk[1]}"
    d = _infer_date(lk[0], lk[1], today)
    if d is not None:
        tl.fresh_days_ago = (today - d).days
        tl.is_stale = tl.fresh_days_ago > _STALE_DAYS
    return tl


def fetch_channel_timeline(
    spreadsheet_url_or_id: str, channel_name: str, today: date | None = None
) -> ChannelTimeline:
    """チャンネル名から個別 TRACK_ タブを解決・取得して時系列指標を返す（オンデマンド用）。"""
    sid = _extract_sheet_id(spreadsheet_url_or_id)
    if not sid:
        return ChannelTimeline(channel_name=channel_name, error="spreadsheet ID 不正")
    gid = resolve_track_gid(sid, channel_name)
    if gid is None:
        gid = resolve_track_gid(sid, channel_name, refresh=True)
    if gid is None:
        return ChannelTimeline(
            channel_name=channel_name,
            error=f"個別タブ {_TRACK_PREFIX}{channel_name} が見つかりません",
        )
    url = f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"
    try:
        rows = fetch_csv(url)
    except SheetFetchError as e:
        return ChannelTimeline(channel_name=channel_name, gid=gid, error=str(e))
    tl = parse_channel_timeline(rows, channel_name, today=today)
    tl.gid = gid
    return tl


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


# ─── 新着チャンネル検知 ──────────────────────────────

def _parse_loose_date(s: str, today: date | None = None) -> date | None:
    """master の日付（"26/5/27" / "2026/05/29" / "26/06/01"）を date に。

    2桁年は 2000+YY に展開。結果が今日より未来なら 1 年引く
    （シート表示の年が曖昧で、26/11/23 のような未来日付＝実際は前年のため）。
    """
    if not s:
        return None
    s = s.strip().replace("-", "/").replace(".", "/")
    m = re.match(r"^(\d{2,4})/(\d{1,2})/(\d{1,2})$", s)
    if not m:
        return None
    y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        d = date(y, mo, dd)
    except ValueError:
        return None
    if today is None:
        today = date.today()
    if d > today:
        try:
            d = date(y - 1, mo, dd)
        except ValueError:
            pass
    return d


def detect_new_channels(
    entries: list[GrowthEntry],
    today: date | None = None,
    new_within_days: int = 14,
) -> list[GrowthEntry]:
    """追跡開始日が直近 N 日以内のチャンネルを「新着」として抽出。

    全 entries に is_new / days_tracked を副作用で付与（identify_hot_channels が
    score を付与するのと同じ流儀）。戻り値は新着のみを「日次登録増 → 登録者数」降順。
    新着判定は master の tracking_start に依存するため、T2 のスナップショット差分
    （前回比で新規出現）と併用するとより厳密になる。
    """
    if today is None:
        today = date.today()
    new_list = []
    for e in entries:
        d = _parse_loose_date(e.tracking_start, today)
        if d is None:
            e.days_tracked = -1
            e.is_new = False
            continue
        e.days_tracked = (today - d).days
        e.is_new = 0 <= e.days_tracked <= new_within_days
        if e.is_new:
            new_list.append(e)
    new_list.sort(key=lambda e: (e.daily_sub_change, e.subscribers), reverse=True)
    return new_list


# ─── 日次スナップショット（新着ch / 新作投稿の差分イベント記録）──────

_SNAPSHOT_DIR = os.path.join(_CONFIG_DIR, "track_snapshots")
_SNAP_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def snapshot_growth(entries: list[GrowthEntry], today: date | None = None) -> dict:
    """CHANNEL_TRACK の現在値を日次スナップショット dict 化（差分検知のソース）。"""
    if today is None:
        today = date.today()
    channels = {}
    for e in entries:
        channels[e.channel_name] = {
            "subs": e.subscribers,
            "views": e.total_views,
            "videos": e.video_count,
            "last_post": e.last_post_date,
            "dsub": e.daily_sub_change,
            "dview": e.daily_view_change,
            "rate": e.growth_rate,
            "start": e.tracking_start,
        }
    return {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(channels),
        "channels": channels,
    }


def save_snapshot(snapshot: dict) -> str:
    """スナップショットを ~/.config/orzz/track_snapshots/YYYY-MM-DD.json に原子的保存。"""
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(_SNAPSHOT_DIR, f"{snapshot['date']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load_latest_snapshot(before: str | None = None) -> dict | None:
    """保存済みスナップショットのうち、before（YYYY-MM-DD）より前で最新を読む。"""
    try:
        files = os.listdir(_SNAPSHOT_DIR)
    except FileNotFoundError:
        return None
    dates = []
    for fn in files:
        m = _SNAP_DATE_RE.match(fn)
        if m and (before is None or m.group(1) < before):
            dates.append(m.group(1))
    if not dates:
        return None
    latest = max(dates)
    try:
        with open(os.path.join(_SNAPSHOT_DIR, f"{latest}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def diff_snapshots(prev: dict, curr: dict) -> dict:
    """前回 → 今回のスナップショット差分から新着ch / 新作投稿 / 急伸イベントを抽出。"""
    pc = (prev or {}).get("channels", {})
    cc = (curr or {}).get("channels", {})
    new_channels, dropped, new_videos, surging = [], [], [], []
    for name, c in cc.items():
        p = pc.get(name)
        if p is None:
            new_channels.append({"name": name, "subs": c["subs"],
                                 "views": c["views"], "start": c["start"],
                                 "dsub": c["dsub"], "dview": c["dview"]})
            continue
        # 新作投稿: 直近投稿日が更新 or 動画本数が増加
        posted = (c["last_post"] and c["last_post"] != p.get("last_post")) \
            or (c["videos"] > p.get("videos", 0))
        if posted:
            new_videos.append({"name": name, "last_post": c["last_post"],
                               "prev_post": p.get("last_post", ""),
                               "video_delta": c["videos"] - p.get("videos", 0),
                               "dview": c["dview"]})
        # 急伸: 前回比の純増（master の dsub より信頼できるクロス日差分）
        sub_delta = c["subs"] - p.get("subs", c["subs"])
        view_delta = c["views"] - p.get("views", c["views"])
        if sub_delta > 0 or view_delta > 0:
            surging.append({"name": name, "sub_delta": sub_delta,
                            "view_delta": view_delta, "subs": c["subs"]})
    for name in pc:
        if name not in cc:
            dropped.append(name)
    new_channels.sort(key=lambda x: (x["dsub"], x["subs"]), reverse=True)
    new_videos.sort(key=lambda x: x["dview"], reverse=True)
    surging.sort(key=lambda x: x["sub_delta"], reverse=True)
    return {
        "prev_date": (prev or {}).get("date"),
        "curr_date": (curr or {}).get("date"),
        "new_channels": new_channels,
        "dropped_channels": dropped,
        "new_videos": new_videos,
        "surging": surging[:20],
    }


def record_daily_snapshot(
    entries: list[GrowthEntry], today: date | None = None
) -> dict:
    """日次スナップショットを記録し、前回比の差分イベントを返す（同日再実行は冪等）。

    戻り: {snapshot, events, prev_date, first_run}。
    events は前回スナップショットが無い初回は first_run=True で空。
    """
    if today is None:
        today = date.today()
    curr = snapshot_growth(entries, today)
    prev = load_latest_snapshot(before=curr["date"])
    if prev:
        events = diff_snapshots(prev, curr)
        first_run = False
    else:
        events = {"prev_date": None, "curr_date": curr["date"],
                  "new_channels": [], "dropped_channels": [],
                  "new_videos": [], "surging": []}
        first_run = True
    save_snapshot(curr)
    return {"snapshot": curr, "events": events,
            "prev_date": prev["date"] if prev else None, "first_run": first_run}


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
