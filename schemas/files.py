from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class FilesResponse(BaseModel):
    current:  str
    parts:    list[str]
    folders:  list[str]
    files:    list[str]
    can_go_up: bool
    error:    Optional[str] = None


class FileMetadataResponse(BaseModel):
    """API response for GET /api/files/meta — covers both images and video."""
    # Image / video common
    width:           Optional[int]       = None
    height:          Optional[int]       = None
    # Image (PNG ComfyUI)
    workflow:        Optional[Any]       = None
    prompt:          Optional[Any]       = None
    raw_text_chunks: dict[str, str]      = {}
    # Video
    duration:        Optional[float]     = None
    fps:             Optional[float]     = None
    video_codec:     Optional[str]       = None
    comment:         Optional[str]       = None