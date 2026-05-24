#!/usr/bin/env python3
"""動画フォルダ内 thumbnail_state.json の CRUD。

各動画の Image/ フォルダ内に thumbnail_state.json を置き、
生成されたサムネイル候補のスコア・承認状態・評価理由を保持する。

スキーマ:
{
  "version": 1,
  "video_name": "5_HN_260503",
  "updated_at": "ISO8601",
  "approval_mode": "manual" | "conditional" | "auto",
  "score_threshold": 80,
  "thumbnails": {
    "5_HN_260503-v1.png": {
      "filename": "5_HN_260503-v1.png",
      "generated_at": "ISO8601",
      "prompt": "Subject: ...",
      "score_total": 92,
      "score_breakdown": {
        "concept_fit": 28,     // /30
        "trend_fit": 18,       // /20
        "competitor_diff": 22, // /25
        "past_perf": 24        // /25
      },
      "ctr_predict": 8.2,
      "similarity_to_competitors": 18,
      "status": "auto_approved" | "needs_review" | "rejected" | "adopted" | "pending",
      "evaluated_at": "ISO8601",
      "approval_reason": "スコア 92 が閾値 80 を超え自動承認",
      "evaluation_comment": "主役の視認性が高く...",
      "is_adopted": false
    }
  },
  "run_history": [
    {
      "run_id": "RUN_20260516_1021",
      "started_at": "...",
      "finished_at": "...",
      "generated": ["v1.png", "v2.png"],
      "approved": ["v1.png"],
      "errors": []
    }
  ]
}
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

STATE_FILENAME = "thumbnail_state.json"
SCHEMA_VERSION = 1

# 承認モード
APPROVAL_MODES = ("manual", "conditional", "auto")
DEFAULT_APPROVAL_MODE = "conditional"
DEFAULT_SCORE_THRESHOLD = 80
DEFAULT_CTR_THRESHOLD = 7.0
DEFAULT_SIMILARITY_MAX = 25

# ステータス
STATUSES = ("pending", "auto_approved", "needs_review", "rejected", "adopted", "failed")


def state_path(image_dir: Path) -> Path:
    return image_dir / STATE_FILENAME


def _empty_state(video_name: str = "") -> dict:
    return {
        "version": SCHEMA_VERSION,
        "video_name": video_name,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "approval_mode": DEFAULT_APPROVAL_MODE,
        "score_threshold": DEFAULT_SCORE_THRESHOLD,
        "ctr_threshold": DEFAULT_CTR_THRESHOLD,
        "similarity_max": DEFAULT_SIMILARITY_MAX,
        "thumbnails": {},
        "run_history": [],
    }


def load_state(image_dir: Path, video_name: str = "") -> dict:
    """thumbnail_state.json を読む。無ければ空 state を返す。"""
    sp = state_path(image_dir)
    if not sp.exists():
        return _empty_state(video_name or image_dir.parent.name)
    try:
        d = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return _empty_state(video_name)
        # マイグレーション余地
        d.setdefault("version", SCHEMA_VERSION)
        d.setdefault("thumbnails", {})
        d.setdefault("run_history", [])
        d.setdefault("approval_mode", DEFAULT_APPROVAL_MODE)
        d.setdefault("score_threshold", DEFAULT_SCORE_THRESHOLD)
        d.setdefault("ctr_threshold", DEFAULT_CTR_THRESHOLD)
        d.setdefault("similarity_max", DEFAULT_SIMILARITY_MAX)
        if video_name and not d.get("video_name"):
            d["video_name"] = video_name
        return d
    except Exception:
        return _empty_state(video_name)


def save_state(image_dir: Path, state: dict) -> None:
    """atomic write (tmp → rename)。"""
    image_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    sp = state_path(image_dir)
    # tmp ファイルに書いてから rename
    fd, tmp = tempfile.mkstemp(prefix=".thumbnail_state_", suffix=".tmp", dir=str(image_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sp)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def get_thumbnail(image_dir: Path, filename: str) -> dict:
    """1 件分の状態。無ければ {}。"""
    state = load_state(image_dir)
    return (state.get("thumbnails") or {}).get(filename, {})


def upsert_thumbnail(image_dir: Path, filename: str, fields: dict,
                     video_name: str = "") -> dict:
    """1 件分の状態をマージ更新して保存。返り値は最新 state。"""
    state = load_state(image_dir, video_name=video_name)
    thumbs = state.setdefault("thumbnails", {})
    cur = thumbs.get(filename, {"filename": filename})
    cur.update(fields)
    cur["filename"] = filename
    thumbs[filename] = cur
    save_state(image_dir, state)
    return state


def upsert_many(image_dir: Path, entries: list[dict], video_name: str = "") -> dict:
    """複数件まとめてマージ更新。"""
    state = load_state(image_dir, video_name=video_name)
    thumbs = state.setdefault("thumbnails", {})
    for entry in entries:
        fn = entry.get("filename")
        if not fn:
            continue
        cur = thumbs.get(fn, {"filename": fn})
        cur.update(entry)
        cur["filename"] = fn
        thumbs[fn] = cur
    save_state(image_dir, state)
    return state


def set_status(image_dir: Path, filename: str, status: str,
               reason: str = "", video_name: str = "") -> dict:
    """ステータス更新ヘルパー。"""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    fields = {"status": status}
    if reason:
        fields["approval_reason"] = reason
    if status == "adopted":
        fields["is_adopted"] = True
    elif status == "rejected":
        fields["is_adopted"] = False
    return upsert_thumbnail(image_dir, filename, fields, video_name=video_name)


def update_settings(image_dir: Path, *, approval_mode: Optional[str] = None,
                    score_threshold: Optional[int] = None,
                    ctr_threshold: Optional[float] = None,
                    similarity_max: Optional[int] = None,
                    video_name: str = "") -> dict:
    """承認設定（mode / 閾値）を保存。"""
    state = load_state(image_dir, video_name=video_name)
    if approval_mode and approval_mode in APPROVAL_MODES:
        state["approval_mode"] = approval_mode
    if score_threshold is not None:
        state["score_threshold"] = max(0, min(100, int(score_threshold)))
    if ctr_threshold is not None:
        state["ctr_threshold"] = float(ctr_threshold)
    if similarity_max is not None:
        state["similarity_max"] = max(0, min(100, int(similarity_max)))
    save_state(image_dir, state)
    return state


def append_run(image_dir: Path, run_entry: dict, video_name: str = "") -> dict:
    """実行履歴の末尾追加。最新 50 件まで保持。"""
    state = load_state(image_dir, video_name=video_name)
    runs = state.setdefault("run_history", [])
    runs.append(run_entry)
    state["run_history"] = runs[-50:]
    save_state(image_dir, state)
    return state


def list_by_status(image_dir: Path, status: str) -> list[dict]:
    """指定 status の thumbnails をリスト。"""
    state = load_state(image_dir)
    return [v for v in (state.get("thumbnails") or {}).values()
            if v.get("status") == status]


def cleanup_missing_files(image_dir: Path) -> int:
    """画像ファイルが消えた entry を thumbnails から除去。返り値は除去数。"""
    state = load_state(image_dir)
    thumbs = state.get("thumbnails") or {}
    to_drop = [fn for fn in thumbs if not (image_dir / fn).exists()]
    for fn in to_drop:
        del thumbs[fn]
    if to_drop:
        save_state(image_dir, state)
    return len(to_drop)


def aggregate_summary(image_dir: Path) -> dict:
    """画面表示用の集計サマリ。"""
    state = load_state(image_dir)
    thumbs = list((state.get("thumbnails") or {}).values())
    counts: dict[str, int] = {}
    score_sum = 0
    score_cnt = 0
    for t in thumbs:
        st = t.get("status") or "pending"
        counts[st] = counts.get(st, 0) + 1
        if isinstance(t.get("score_total"), (int, float)):
            score_sum += t["score_total"]
            score_cnt += 1
    avg = round(score_sum / score_cnt, 1) if score_cnt else 0.0
    return {
        "total": len(thumbs),
        "counts_by_status": counts,
        "average_score": avg,
        "adopted_count": sum(1 for t in thumbs if t.get("is_adopted")),
        "approval_mode": state.get("approval_mode"),
        "score_threshold": state.get("score_threshold"),
    }


# ─── CLI (デバッグ用) ─────────────────────
def _cli():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--image-dir", required=True)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--cleanup", action="store_true")
    args = p.parse_args()
    d = Path(args.image_dir)
    if args.cleanup:
        n = cleanup_missing_files(d)
        print(f"cleaned {n} missing entries")
    if args.summary:
        print(json.dumps(aggregate_summary(d), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
