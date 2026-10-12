"""Optional router; existing app auth must wrap it. Never accepts API keys."""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from app_fal_video import GateError, VideoService, VideoSpec, PRICE_DATE, PRICE_VALID_UNTIL, PROMO, NORMAL


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: VideoSpec
    max_usd: str = Field(min_length=1, max_length=30)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str
    approve_transfer: StrictBool
    approved_max_usd: str = Field(min_length=1, max_length=30)


class SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default 422 includes submitted values. Never reflect an
                # accidentally pasted credential/unknown key field to API consumers.
                return JSONResponse({"detail": "入力形式が不正です。APIキーはこのAPIへ送信せず、サーバーの環境変数に設定してください。"}, status_code=422)
        return handle


@lru_cache(maxsize=1)
def configured_service():
    root = runtime_root()
    if not os.path.isabs(root):
        raise GateError("falの専用保存先APP_FAL_RUNTIME_ROOTが未設定です。既存制作素材とは別の絶対パスを設定してください。")
    return VideoService(root, role=os.environ.get("APP_FAL_EXECUTION_ROLE", "mac"),
                        allow_paid_submit=os.environ.get("APP_FAL_PAID_SUBMIT_ENABLED", "0") == "1")


def runtime_root():
    root = os.environ.get("APP_FAL_RUNTIME_ROOT")
    if root:
        return root
    # Existing local profile, separate from the shared repo and original assets.
    from _app_config import resolve_config_dir
    return str(resolve_config_dir() / "fal-video")


def make_router(service_factory=configured_service):
    router = APIRouter(prefix="/api/fal-video", tags=["fal-video"], route_class=SafeValidationRoute)

    def call(fn):
        try:
            return fn(service_factory())
        except GateError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            # Never serialize an exception containing FAL_KEY/upstream request details.
            raise HTTPException(503, "fal連携を処理できません。サーバー設定と保存先を確認してください。") from None

    def same_origin(request):
        origin = request.headers.get("origin")
        if (origin and origin != f"{request.url.scheme}://{request.url.netloc}") or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "同じAutomation Studio画面から操作してください。")

    @router.get("/capabilities")
    def capabilities():
        role = os.environ.get("APP_FAL_EXECUTION_ROLE", "mac")
        return {"model": "minimax/h3-max-turbo/image-to-video", "role": role,
                "key_configured": bool(os.environ.get("FAL_KEY")), "uploads_supported": True,
                "paid_submit_enabled": role == "cloud" or os.environ.get("APP_FAL_PAID_SUBMIT_ENABLED", "0") == "1",
                "compute_location": "fal-cloud", "controller_location": role,
                "local_history_configured": os.path.isabs(runtime_root()),
                "price": {"checked_at": PRICE_DATE, "valid_until": PRICE_VALID_UNTIL,
                          "promo_per_second": {k: str(v) for k, v in PROMO.items()},
                          "normal_per_second": {k: str(v) for k, v in NORMAL.items()}, "output_overrun_allowance": 0.7},
                "production_tested": False, "automatic_resubmit": False}

    @router.post("/images/upload")
    async def upload_image(request: Request, image: UploadFile = File(...),
                           approved_sha256: str = Form(...), confirmation: str = Form(...)):
        same_origin(request)
        if request.headers.get("x-fal-intent") != "image-transfer-approved" or confirmation != "UPLOAD_SELECTED_PNG":
            raise HTTPException(403, "選択画像のfal転送を明示承認してください。")
        data = await image.read(8 * 1024 * 1024 + 1)
        await image.close()
        return await run_in_threadpool(call, lambda s: s.upload_png(data, approved_sha256=approved_sha256, approve_transfer=True))

    @router.post("/quotes")
    def quote(body: QuoteRequest, request: Request):
        same_origin(request)
        try:
            budget = Decimal(body.max_usd)
        except InvalidOperation:
            raise HTTPException(400, "予算上限をドルの数値で指定してください。") from None
        return call(lambda s: s.quote(body.spec, budget))

    @router.post("/jobs/{job_id}/submit")
    def submit(job_id: str, body: SubmitRequest, request: Request):
        same_origin(request)
        if request.headers.get("x-fal-intent") != "user-approved":
            raise HTTPException(403, "見積りの明示承認後に送信してください。")
        return call(lambda s: s.submit(job_id, confirmation=body.confirmation,
                    approve_transfer=body.approve_transfer, approved_max_usd=body.approved_max_usd))

    @router.get("/jobs")
    def history():
        return call(lambda s: {"jobs": s.history()})

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return call(lambda s: s.get(job_id))

    @router.post("/jobs/{job_id}/refresh")
    def refresh(job_id: str, request: Request):
        same_origin(request)
        return call(lambda s: s.refresh(job_id))

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        same_origin(request)
        return call(lambda s: s.cancel(job_id))

    @router.post("/jobs/{job_id}/download")
    def download(job_id: str, request: Request):
        same_origin(request)
        return call(lambda s: s.download(job_id))

    @router.post("/jobs/{job_id}/normalize")
    def normalize(job_id: str, request: Request):
        same_origin(request)
        return call(lambda s: s.normalize_720p(job_id))

    @router.get("/jobs/{job_id}/file")
    def file(job_id: str, normalized: bool = False):
        path = call(lambda s: s.file(job_id, normalized=normalized))
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    return router


router = make_router()
