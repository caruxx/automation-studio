#!/usr/bin/env python3
"""ベンチマーク分析軸ルータ（D9 第2段で app.py から分離）。

サムネ/コンセプト/タイトル/投稿文の4軸の取得・実行(非同期)エンドポイント。
重い分析処理は app_benchmark_thumbnail/concept/title/description をハンドラ内で
ローカル import する（app.py 慣習踏襲）。app_core の config ローダのみ依存。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from app_core import get_suno_config, get_dashboard_config

router = APIRouter()


# ─── API: ベンチマーク・サムネイル軸（Phase 1） ───
# 既存 competitor_analysis_cache.json を入力に、サムネ画像を DL → Vision 分析 →
# picked リストを Flow / Image2 への参照画像として再利用する。

class BenchThumbRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    dl_only: Optional[bool] = False  # true なら分析スキップ
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

class BenchThumbPickedUpdate(BaseModel):
    picked: List[str]  # videoId のリスト

import threading as _bt_threading

_bt_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bt_set_status(**kw):
    _bt_status.update(kw)

def _bt_now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

@router.get("/api/benchmark/thumbnail")
def api_benchmark_thumbnail_get():
    """サムネ分析の現在のキャッシュを返す（画像本体は別エンドポイント）。"""
    import app_benchmark_thumbnail as _bt
    cache = _bt.load_cache()
    return {
        "status": "ok",
        "running": _bt_status.get("running", False),
        "phase": _bt_status.get("phase", ""),
        "msg": _bt_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "channels": cache.get("channels", []),
        "analysis": cache.get("analysis", {}),
        "picked": cache.get("picked", []),
    }

@router.get("/api/benchmark/thumbnail/image/{channel_id}/{video_id}")
def api_benchmark_thumbnail_image(channel_id: str, video_id: str):
    """ローカル保存したサムネ画像を返す（UI のグリッド表示用）。"""
    import app_benchmark_thumbnail as _bt
    safe_ch = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel_id)[:64]
    safe_vid = "".join(c if c.isalnum() or c in "-_" else "_" for c in video_id)[:32]
    fp = _bt.THUMBS_DIR / safe_ch / f"{safe_vid}.jpg"
    if not fp.exists():
        raise HTTPException(404, "thumbnail not found")
    return FileResponse(fp, media_type="image/jpeg")

@router.post("/api/benchmark/thumbnail/run")
def api_benchmark_thumbnail_run(req: BenchThumbRunRequest = BenchThumbRunRequest()):
    """サムネ DL + Vision 分析を非同期で開始。結果は GET /api/benchmark/thumbnail で取得。"""
    if _bt_status.get("running"):
        return {"status": "already_running", **_bt_status}

    import app_benchmark_thumbnail as _bt
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    def _worker():
        _bt_set_status(running=True, phase="downloading", msg="サムネを DL 中...",
                       started_at=_bt_now_iso(), finished_at="")
        try:
            channels = _bt.download_thumbnails()
            if not channels:
                raise RuntimeError("DL 対象がありません。先に競合データ取得を実行してください。")
            cache = _bt.load_cache()
            cache["channels"] = channels
            cache["generated_at"] = _bt_now_iso()
            cache["picked"] = _bt._filter_valid_picks(cache.get("picked", []), channels)
            _bt.save_cache(cache)

            if req.dl_only:
                _bt_set_status(running=False, phase="done", msg=f"DL 完了 ({sum(len(c.get('thumbnails',[])) for c in channels)} 枚)",
                               finished_at=_bt_now_iso())
                return

            _bt_set_status(phase="analyzing", msg="Claude Vision で分析中...")
            _existing_pc = (cache.get("analysis") or {}).get("per_channel", {}) or {}
            analysis = _bt.analyze_channels(channels, cli_cmd=cli_cmd,
                                            per_channel_cap=req.per_channel_cap or 8,
                                            only_channel_ids=req.channel_ids,
                                            skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged),
                                            existing_per_channel=_existing_pc)
            cache["analysis"] = analysis
            _bt.save_cache(cache)
            _bt_set_status(running=False, phase="done", msg="分析完了", finished_at=_bt_now_iso())
        except Exception as e:
            _bt_set_status(running=False, phase="error", msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}

@router.put("/api/benchmark/thumbnail/picked")
def api_benchmark_thumbnail_set_picked(req: BenchThumbPickedUpdate):
    import app_benchmark_thumbnail as _bt
    cleaned = _bt.set_picked(req.picked or [])
    return {"status": "ok", "picked": cleaned, "count": len(cleaned)}

@router.get("/api/benchmark/thumbnail/picked")
def api_benchmark_thumbnail_get_picked():
    import app_benchmark_thumbnail as _bt
    return {"status": "ok", "details": _bt.get_picked_details()}


# ─── API: ベンチマーク・コンセプト軸（Phase 2） ───
# テキスト解析中心（タイトル / タグ / desc）→ themes / emotional_jobs / scene_anchors

class BenchConceptRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    channel_ids: Optional[List[str]] = None  # 選択ch名/id 限定（未指定=全件）
    skip_unchanged: Optional[bool] = True    # 入力指紋が一致なら再分析せず流用

_bc_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bc_set_status(**kw):
    _bc_status.update(kw)

@router.get("/api/benchmark/concept")
def api_benchmark_concept_get():
    """コンセプト軸の現在のキャッシュを返す。"""
    import app_benchmark_concept as _bc
    cache = _bc.load_cache()
    return {
        "status": "ok",
        "running": _bc_status.get("running", False),
        "phase": _bc_status.get("phase", ""),
        "msg": _bc_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@router.post("/api/benchmark/concept/run")
def api_benchmark_concept_run(req: BenchConceptRunRequest = BenchConceptRunRequest()):
    """コンセプト分析を非同期で開始。"""
    if _bc_status.get("running"):
        return {"status": "already_running", **_bc_status}

    import app_benchmark_concept as _bc
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")

    def _worker():
        _bc_set_status(running=True, phase="analyzing",
                       msg="チャンネル別 + 横断分析を実行中...",
                       started_at=_bt_now_iso(), finished_at="")
        try:
            _bc.run_full(cli_cmd=cli_cmd,
                         per_channel_cap=req.per_channel_cap or 8,
                         self_persona=persona,
                         only_channel_ids=req.channel_ids,
                         skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            _bc_set_status(running=False, phase="done", msg="完了",
                           finished_at=_bt_now_iso())
        except Exception as e:
            _bc_set_status(running=False, phase="error",
                           msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}


# ─── API: ベンチマーク・タイトル軸（Phase 3） ───
# タイトル群 → patterns / formulas / keywords / hooks / scaffolds

class BenchTitleRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 10
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

_btitle_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _btitle_set_status(**kw):
    _btitle_status.update(kw)

@router.get("/api/benchmark/title")
def api_benchmark_title_get():
    import app_benchmark_title as _btm
    cache = _btm.load_cache()
    return {
        "status": "ok",
        "running": _btitle_status.get("running", False),
        "phase": _btitle_status.get("phase", ""),
        "msg": _btitle_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@router.post("/api/benchmark/title/run")
def api_benchmark_title_run(req: BenchTitleRunRequest = BenchTitleRunRequest()):
    if _btitle_status.get("running"):
        return {"status": "already_running", **_btitle_status}

    import app_benchmark_title as _btm
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")

    def _worker():
        _btitle_set_status(running=True, phase="analyzing",
                           msg="タイトル分析を実行中...",
                           started_at=_bt_now_iso(), finished_at="")
        try:
            _btm.run_full(cli_cmd=cli_cmd,
                          per_channel_cap=req.per_channel_cap or 10,
                          self_persona=persona,
                          only_channel_ids=req.channel_ids,
                          skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            _btitle_set_status(running=False, phase="done", msg="完了",
                               finished_at=_bt_now_iso())
        except Exception as e:
            _btitle_set_status(running=False, phase="error",
                               msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}


# ─── API: ベンチマーク・投稿文（説明文）軸 ───
# 指定チャンネルの説明文構成 → per_channel + aggregate(recommendation_for_self)
# (a)既存メタ改善 と (b)新規投稿文構成案 へ還流。説明文は API rivals 経路のみ取得可。

class BenchDescriptionRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    refetch_full: Optional[bool] = True  # 軸 run 時に full 説明文を再取得（500字版の欠落を補う）
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

_bdesc_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bdesc_set_status(**kw):
    _bdesc_status.update(kw)

@router.get("/api/benchmark/description")
def api_benchmark_description_get():
    import app_benchmark_description as _bdm
    cache = _bdm.load_cache()
    return {
        "status": "ok",
        "running": _bdesc_status.get("running", False),
        "phase": _bdesc_status.get("phase", ""),
        "msg": _bdesc_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "no_data": cache.get("no_data", False),
        "reason": cache.get("reason", ""),
        "source": cache.get("source", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@router.post("/api/benchmark/description/run")
def api_benchmark_description_run(req: BenchDescriptionRunRequest = BenchDescriptionRunRequest()):
    if _bdesc_status.get("running"):
        return {"status": "already_running", **_bdesc_status}

    import app_benchmark_description as _bdm
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")
    refetch_full = True if req.refetch_full is None else bool(req.refetch_full)

    def _worker():
        _bdesc_set_status(running=True, phase="analyzing",
                          msg=("full 説明文を再取得して分析中..." if refetch_full else "投稿文構成を分析中..."),
                          started_at=_bt_now_iso(), finished_at="")
        try:
            result = _bdm.run_full(cli_cmd=cli_cmd,
                                   per_channel_cap=req.per_channel_cap or 8,
                                   self_persona=persona,
                                   refetch_full=refetch_full,
                                   only_channel_ids=req.channel_ids,
                                   skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            if result.get("no_data"):
                _bdesc_set_status(running=False, phase="done",
                                  msg=(result.get("reason") or "説明文データなし"),
                                  finished_at=_bt_now_iso())
            else:
                _bdesc_set_status(running=False, phase="done", msg="完了",
                                  finished_at=_bt_now_iso())
        except Exception as e:
            _bdesc_set_status(running=False, phase="error",
                              msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}
