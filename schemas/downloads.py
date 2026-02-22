from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MirrorSchema(BaseModel):
    url: str
    priority: Optional[int] = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)


class ExtractOptionsSchema(BaseModel):
    format: str = "zip"
    destination: Optional[str] = None
    remove_archive: bool = False
    password: Optional[str] = None


class AddDownloadRequest(BaseModel):
    file_name: Optional[str] = None
    folder_path: Optional[str] = None
    mirrors: list[MirrorSchema] = Field(default_factory=list)
    expected_hash: Optional[str] = None
    hash_algorithm: str = "sha256"
    extract: Optional[ExtractOptionsSchema] = None


class AddDownloadResponse(BaseModel):
    success: bool
    task_id: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


class TaskSchema(BaseModel):
    task_id: str
    url: Optional[str] = None
    file_name: Optional[str] = None
    folder_path: Optional[str] = None
    filepath: Optional[str] = None
    status: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress_percent: float = 0.0
    speed: Optional[float] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    expected_hash: Optional[str] = None
    mirrors_count: int = 0


class AllTasksResponse(BaseModel):
    tasks: list[TaskSchema]


class TaskProgressResponse(BaseModel):
    task_id: str
    url: Optional[str] = None
    status: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress_percent: float = 0.0


class TaskActionResponse(BaseModel):
    success: bool
    message: str


class VerifyFileResponse(BaseModel):
    task_id: str
    is_valid: bool
    expected_hash: Optional[str] = None
    actual_hash: Optional[str] = None
    algorithm: Optional[str] = None


class SetConcurrencyRequest(BaseModel):
    max_concurrent: int


class SetConcurrencyResponse(BaseModel):
    success: bool
    max_concurrent: Optional[int] = None
    message: Optional[str] = None
    error: Optional[str] = None


class GetConcurrencyResponse(BaseModel):
    success: bool
    max_concurrent: Optional[int] = None
    error: Optional[str] = None

class DeleteDownloadRequest(BaseModel):
    delete_file: bool = False

class ExtractFileResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None