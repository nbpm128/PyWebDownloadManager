from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class UrlMetaRequest(BaseModel):
    """Request body for POST /api/url-meta"""

    url: str
    # Resolved auth headers to forward (e.g. Authorization, Cookie)
    headers: Optional[dict[str, str]] = None
    # Cookies to forward (merged into Cookie header)
    cookies: Optional[dict[str, str]] = None


class UrlMetaItem(BaseModel):
    """Metadata extracted for a single file URL."""

    id: str
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    folder_path: Optional[str] = None
    checksum: Optional[str] = None
    hash_algorithm: Optional[str] = None
    description: Optional[str] = None
    mirrors: list[dict[str, Any]] = []


class UrlMetaResponse(BaseModel):
    """Response from POST /api/url-meta"""

    success: bool
    item: Optional[UrlMetaItem] = None
    # Non-fatal warning (e.g. metadata unavailable, item still included)
    warning: Optional[str] = None
    error: Optional[str] = None