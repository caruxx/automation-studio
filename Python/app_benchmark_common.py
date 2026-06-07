#!/usr/bin/env python3
"""ベンチマーク各軸（concept/title/thumbnail/description）共通の増分・スキップ補助。

無駄な Claude CLI 消費を削るための 2 機構:
  - only_channel_ids: 選択チャンネルだけ分析（未指定=全件）
  - skip_unchanged: 入力 fingerprint が既存結果と一致なら再分析せず流用
保存は「既存 per_channel ∪ 今回」のマージ（選択外の既存結果を消さない）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime


def channel_fingerprint(channel: dict) -> str:
    """チャンネル入力（top/recent 動画の videoId と title 集合）から短い指紋を作る。

    動画の入替・新着・タイトル変更で変化し、views の揺れだけでは変化しない
    （title + videoId 集合のみを対象にすることで再分析の取りこぼしと過剰を両立）。
    """
    items = []
    for key in ("topByViews", "recentUploads"):
        for v in (channel.get(key) or []):
            vid = (v.get("videoId") or "").strip()
            title = (v.get("title") or "").strip()
            items.append(vid or title)
            items.append(title)
    blob = json.dumps(sorted(items), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def plan_channel(ch_id: str, fp: str, existing: dict,
                 only_channel_ids, skip_unchanged: bool, ch_name: str = "") -> str:
    """このチャンネルの処理方針を返す: "carry"（既存流用） / "run"（再分析）。

    - only_channel_ids 指定かつ対象外（id も name も不一致）→ carry
    - skip_unchanged かつ 既存の _meta.fp が一致 → carry
    - それ以外 → run
    UI の選択はチャンネル名で来るため id/name の双方で対象判定する。
    """
    if only_channel_ids is not None and ch_id not in only_channel_ids and (ch_name or "") not in only_channel_ids:
        return "carry"
    ex = existing.get(ch_id) if existing else None
    if skip_unchanged and ex is not None:
        meta = ex.get("_meta") if isinstance(ex, dict) else None
        if isinstance(meta, dict) and meta.get("fp") == fp:
            return "carry"
    return "run"


def stamp_meta(obj: dict, fp: str) -> dict:
    """分析結果に _meta（fingerprint + 生成時刻）を付与して返す。"""
    if isinstance(obj, dict):
        obj["_meta"] = {"fp": fp, "generated_at": datetime.now().isoformat(timespec="seconds")}
    return obj


def normalize_ids(channel_ids) -> set | None:
    """channel_ids（list）を set へ。None/空なら None（=全件）。"""
    if not channel_ids:
        return None
    s = {str(x).strip() for x in channel_ids if str(x).strip()}
    return s or None


def extract_json_object(text):
    """文字列から JSON オブジェクトを抽出（コードフェンス/前後文を無視）。

    ```json ... ``` フェンスを剥がし、最初の '{' から最後の '}' までを
    json.loads。失敗時は末尾カンマを除去して再試行。抽出不能なら None。

    D10: 同型実装が 10 ファイルに重複していたものを 1 関数へ集約。
    各モジュールは旧名のエイリアス（_extract_json / _extract_json_object）で import する。
    """
    import re
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < 0 or end <= start:
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
