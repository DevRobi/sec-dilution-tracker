"""In-memory per-IP rate limiting for auth endpoints."""

import os
import time
from collections import defaultdict
from threading import Lock
from typing import DefaultDict, List

from fastapi import HTTPException, Request, status

_AUTH_RATE_LIMIT_MAX = int(os.getenv("AUTH_RATE_LIMIT_MAX", "10"))
_AUTH_RATE_LIMIT_WINDOW_SECONDS = int(
    os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300")
)

_lock = Lock()
_buckets: DefaultDict[str, List[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def enforce_rate_limit(request: Request, *, scope: str) -> None:
    """
    Raise 429 if this IP exceeded the auth rate limit for `scope`
    (e.g. 'login' or 'signup').
    """
    max_calls = _AUTH_RATE_LIMIT_MAX
    window = _AUTH_RATE_LIMIT_WINDOW_SECONDS
    key = f"{scope}:{_client_ip(request)}"
    now = time.time()
    cutoff = now - window

    with _lock:
        hits = [t for t in _buckets[key] if t > cutoff]
        if len(hits) >= max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please wait a few minutes and try again.",
            )
        hits.append(now)
        _buckets[key] = hits
