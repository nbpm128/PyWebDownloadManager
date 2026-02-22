from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class ConfigMetaSchema(BaseModel):
    name: str
    size: int
    modified: float


class ListConfigsResponse(BaseModel):
    success: bool
    configs: list[ConfigMetaSchema] = []
    error: Optional[str] = None


class LoadConfigResponse(BaseModel):
    success: bool
    config: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class SaveConfigResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    config_name: Optional[str] = None
    error: Optional[str] = None


class DeleteConfigResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None