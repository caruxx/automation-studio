#!/usr/bin/env python3
"""チャンネル動画横断・サムネ一括生成 — ピュアロジック層。

責務:
  - 自動画ごとの「題材キーワード Jaccard」による競合動画マッチング
  - YouTube Data API で取得した数値を combine した concept_hint 文字列の構築
  - Vision 分析 → 5要素 thumbnail_axis への変換ラッパー
  - 既存 Image/ フォルダをスキャンして start_index を決定

サブプロセス起動・進捗ストリームは app.py の async ワーカー側で行う。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ─── 共通: トークナイズ + Jaccard ──────────────────

# ストップワード（日本語 + 英語、サムネ・動画タイトルで頻出すぎて識別性低い語）
_STOPWORDS = {
    # 日本語
    "の", "を", "に", "は", "が", "で", "と", "も", "へ", "や", "など", "から", "まで",
    "こと", "これ", "それ", "ある", "ない", "する", "なる", "ため",
    "動画", "映像", "再生", "音楽", "ミュージック", "サムネ", "サムネイル", "プレイリスト",
    "本", "選", "曲", "分", "時間", "vol", "vlog",
    # 英語
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "these", "those",
    "music", "bgm", "playlist", "mix", "vol", "feat", "ft", "official", "video",
}

_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+"                    # ascii 英数
    r"|[぀-ゟ]+"               # ひらがな塊
    r"|[゠-ヿ]+"               # カタカナ塊
    r"|[一-鿿]+",              # 漢字塊
    re.UNICODE,
)


def _tokenize(text: str) -> set[str]:
    """簡易トークナイザ。日本語は塊ごと、英語は単語ごとに分割。"""
    if not text:
        return set()
    raw = _TOKEN_RE.findall(text.lower())
    return {t for t in raw if t and t not in _STOPWORDS and len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ─── 競合動画マッチング ─────────────────────────────

def match_competitors_by_keyword(
    self_title: str,
    self_concept: str,
    benchmark_cache: dict,
    top_n: int = 4,
    min_score: float = 0.02,
) -> list[dict]:
    """自動画 (title + concept) と benchmark 動画群を題材キーワード Jaccard でマッチ。

    Returns: スコア降順 [{channelId, channelName, videoId, title, viewCount,
                          localPath, match_score}] / TOP N 件
    """
    self_tokens = _tokenize(self_title) | _tokenize(self_concept)
    if not self_tokens:
        return []

    candidates: list[dict] = []
    for ch in (benchmark_cache or {}).get("channels", []) or []:
        ch_id = ch.get("channelId")
        ch_name = ch.get("channelName")
        for t in ch.get("thumbnails", []) or []:
            local_path = t.get("localPath")
            if not local_path or not Path(local_path).exists():
                continue
            comp_title = t.get("title", "")
            comp_tokens = _tokenize(comp_title)
            score = _jaccard(self_tokens, comp_tokens)
            if score < min_score:
                continue
            candidates.append({
                "channelId": ch_id,
                "channelName": ch_name,
                "videoId": t.get("videoId"),
                "title": comp_title,
                "viewCount": int(t.get("viewCount", 0) or 0),
                "likeCount": int(t.get("likeCount", 0) or 0),
                "publishedAt": t.get("publishedAt", ""),
                "localPath": local_path,
                "match_score": round(score, 3),
            })

    # スコア降順 → 同点は viewCount 降順（より「効いてる」competitor を優先）
    candidates.sort(key=lambda x: (-x["match_score"], -x["viewCount"]))
    return candidates[:top_n]


# ─── concept_hint 構築（Phase B 数値統合） ────────────

def build_per_video_concept_hint(
    video_title: str,
    video_concept: str,
    self_stats: Optional[dict] = None,
    peer_avg_views: Optional[float] = None,
    concept_hint_override: str = "",
) -> str:
    """Vision に渡す concept_hint を組み立てる。

    Args:
        self_stats: YouTube Data API で取得した自動画 metadata
                    {viewCount, likeCount, commentCount, publishedAt}（任意）
        peer_avg_views: 競合 matched videos の views 平均（任意）
        concept_hint_override: ユーザーが UI で上書きした場合はこれを最優先
    """
    if concept_hint_override.strip():
        return concept_hint_override.strip()[:600]

    parts: list[str] = []
    title = (video_title or "").strip()
    if title:
        parts.append(f"自動画タイトル「{title}」")

    # Phase B: 数値統合
    if self_stats:
        try:
            sv = int(self_stats.get("viewCount", 0) or 0)
            sl = int(self_stats.get("likeCount", 0) or 0)
            if sv > 0:
                num_part = f"views={sv:,}, likes={sl:,}"
                if peer_avg_views and peer_avg_views > 0:
                    ratio = sv / peer_avg_views * 100
                    num_part += f", peer avg比 {ratio:.0f}%"
                parts.append(f"（{num_part}）")
        except Exception:
            pass

    concept = (video_concept or "").strip()
    if concept:
        # concept.txt は数百〜数千字あり得るので、先頭 300 字に切る
        parts.append("。" + concept[:300])

    return "".join(parts)[:600]


# ─── start_index 自動算出 ────────────────────────────

_EXT_ALT = r"(?:png|jpe?g|webp)"


def scan_start_index(image_dir: Path, prefix: str) -> tuple[int, int]:
    """既存 Image/ フォルダから prefix-v{N}.{ext} の最大 v 番号を検出。

    n_per_prompt > 1 で生成された prefix-v{N}-{m}.{ext} 連番ファイルも認識する。

    Returns: (start_index, existing_count)
        start_index: 次に使うべき v 番号（max+1、または 1）
        existing_count: マッチした既存ファイル数
    """
    if not image_dir.is_dir() or not prefix:
        return 1, 0
    # prefix は app_image_prompt 側で先頭 20 字に切られるので、同じく切る
    truncated = (prefix[:20].rstrip("-")) or "image"
    pat = re.compile(
        rf"^{re.escape(truncated)}-v(\d+)(?:-\d+)?\.{_EXT_ALT}$",
        re.IGNORECASE,
    )
    max_v = 0
    existing = 0
    try:
        for f in image_dir.iterdir():
            if not f.is_file():
                continue
            m = pat.match(f.name)
            if m:
                existing += 1
                try:
                    max_v = max(max_v, int(m.group(1)))
                except ValueError:
                    pass
    except Exception:
        return 1, 0
    return (max_v + 1 if max_v > 0 else 1), existing


# ─── 動画フォルダの context 取得 ─────────────────────

def read_video_context(video_dir: Path) -> dict:
    """動画フォルダから title / concept / video_id を読む。

    対応ファイル (優先順):
      - youtube_title.txt (タイトル本文)
      - youtube_upload.json の "title" (アップロード済みの場合のフォールバック)
      - concept.txt / concept_jp.txt / concept_en.txt (コンセプトメモ)
      - youtube_description.txt (description を補助テキストとして concept に追加)
      - youtube_tags.txt (タグ・任意)
    """
    def _read(name: str) -> str:
        p = video_dir / name
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    # title: txt 優先 → アップロード済みなら upload.json の title → なければフォルダ名
    title = _read("youtube_title.txt")
    description = ""
    video_id = ""
    up_path = video_dir / "youtube_upload.json"
    if up_path.exists():
        try:
            import json
            d = json.loads(up_path.read_text(encoding="utf-8"))
            video_id = d.get("video_id", "") or ""
            if not title:
                title = (d.get("title") or "").strip()
            description = (d.get("description") or "").strip()
        except Exception:
            pass
    if not title:
        title = video_dir.name

    concept = _read("concept.txt") or _read("concept_jp.txt") or _read("concept_en.txt")
    # concept が無くて description があれば description を補助テキストとして使う
    if not concept and description:
        concept = description[:500]

    tags = _read("youtube_tags.txt")
    # tags はトークン語彙を増やすため title に統合
    extended_title = title
    if tags:
        extended_title = title + " " + tags.replace("\n", " ")

    return {
        "video_name": video_dir.name,
        "title": title,
        "title_extended": extended_title,   # マッチング用 (title + tags)
        "concept": concept,
        "description": description,
        "tags": tags,
        "video_id": video_id,
        "video_dir": str(video_dir),
        "has_title": bool(_read("youtube_title.txt")) or bool(video_id),
        "has_concept": bool(concept),
    }


# ─── matched_competitors の集計 ─────────────────────

def average_views(matched: list[dict]) -> float:
    """matched competitor の views 平均（Phase B の peer_avg_views 計算用）。"""
    if not matched:
        return 0.0
    views = [int(m.get("viewCount", 0) or 0) for m in matched]
    views = [v for v in views if v > 0]
    return sum(views) / len(views) if views else 0.0


def matched_local_paths(matched: list[dict]) -> list[Path]:
    """matched 中で実在する localPath だけ抽出して Path に変換。"""
    out: list[Path] = []
    for m in matched or []:
        p = m.get("localPath")
        if p:
            pp = Path(p)
            if pp.exists():
                out.append(pp)
    return out
