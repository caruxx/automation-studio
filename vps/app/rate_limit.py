"""Small process-local rate limiter for defense in depth.

State is not shared between uvicorn workers and is lost on restart. For login,
Nginx ``login_zone`` is the primary IP rate limit; AUTH_LOGIN_IP_MAX_ATTEMPTS
and the other application-side limits are supplemental controls only.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status


_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(
    key: str,
    *,
    max_attempts: int,
    window_seconds: int,
    detail: str = "短時間に試行回数が多すぎます。しばらく待ってから再試行してください。",
) -> None:
    now = time.time()
    cutoff = now - window_seconds
    with _lock:
        bucket = _attempts[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
        bucket.append(now)


def reset_rate_limit(key: str) -> None:
    with _lock:
        _attempts.pop(key, None)


def clear_rate_limits() -> None:
    """Clear process-local state between isolated tests."""
    with _lock:
        _attempts.clear()
