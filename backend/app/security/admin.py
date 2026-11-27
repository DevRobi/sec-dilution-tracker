"""Protect operational admin routes with a shared secret."""

import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

_ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()


def require_admin_key(x_admin_key: Optional[str] = Header(default=None)) -> None:
    """
    Require `X-Admin-Key` header matching ADMIN_API_KEY.
    Fails closed when ADMIN_API_KEY is not configured.
    """
    if not _ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin operations are not configured.",
        )
    if not x_admin_key or not secrets.compare_digest(x_admin_key, _ADMIN_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin credentials.",
        )
