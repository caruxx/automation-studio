"""Automation Studio VPS control-plane API."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.auth import router as auth_router
from .api.channels import router as channels_router
from .api.jobs import router as jobs_router
from .api.oauth import router as oauth_router
from .api.users import router as users_router, worker_token_router
from .api.worker import router as worker_router


def create_app() -> FastAPI:
    app = FastAPI(title="Automation Studio VPS", version="0.1.0")
    origins = [
        item.strip()
        for item in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Channel-Id"],
    )
    app.include_router(auth_router)
    app.include_router(channels_router)
    app.include_router(oauth_router)
    app.include_router(users_router)
    app.include_router(worker_token_router)
    app.include_router(jobs_router)
    app.include_router(worker_router)

    @app.get("/api/health", tags=["system"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
