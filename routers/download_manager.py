from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.templating import Jinja2Templates

from schemas.configs import (
    DeleteConfigResponse,
    ListConfigsResponse,
    LoadConfigResponse,
    SaveConfigResponse,
)
from schemas.downloads import (
    AddDownloadRequest,
    AddDownloadResponse,
    AllTasksResponse,
    TaskActionResponse,
    TaskProgressResponse,
    VerifyFileResponse, SetConcurrencyResponse, SetConcurrencyRequest, GetConcurrencyResponse, DeleteDownloadRequest,
    ExtractFileResponse,
)
from services.config_loader_service import ConfigLoaderService
from services.download_manager_service import DownloadManagerService

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependencies (module-level singletons — swap for DI container if needed)
# ---------------------------------------------------------------------------

_templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=_templates_dir)

dm_service = DownloadManagerService()

_presets_dir = os.path.join(os.path.dirname(__file__), "..", "presets")
config_service = ConfigLoaderService(presets_path=_presets_dir)


# ===========================================================================
# Page
# ===========================================================================

@router.get("/dm")
async def download_manager_page(request: Request):
    return templates.TemplateResponse("download_manager.html", {"request": request, "active_page": "dm"})


# ===========================================================================
# Downloads — listing & control
# ===========================================================================

@router.get("/api/downloads/all", response_model=AllTasksResponse)
async def get_all_downloads():
    return dm_service.get_all_tasks()


@router.post("/api/downloads/start", response_model=TaskActionResponse)
async def start_downloads():
    return await dm_service.start_downloads()


@router.post("/api/downloads/stop", response_model=TaskActionResponse)
async def stop_all_downloads():
    return await dm_service.stop_all_downloads()


@router.post("/api/downloads/set-concurrency", response_model=SetConcurrencyResponse)
async def set_concurrency(request: SetConcurrencyRequest):
    return await dm_service.set_concurrency(request.max_concurrent)


@router.get("/api/downloads/concurrency", response_model=GetConcurrencyResponse)
async def get_concurrency():
    return dm_service.get_concurrency()


# ===========================================================================
# Downloads — add (two transports: JSON body + GET query-string)
# ===========================================================================

@router.post("/api/downloads/add", response_model=AddDownloadResponse)
async def add_download(request: AddDownloadRequest):
    return await dm_service.add_download(request)


@router.post("/api/downloads/{task_id}/extract", response_model=ExtractFileResponse)
async def extract_download(task_id: str):
    return await dm_service.extract_file(task_id)

# ===========================================================================
# Downloads — per-task actions
# ===========================================================================

@router.get("/api/downloads/{task_id}/progress", response_model=TaskProgressResponse)
async def get_download_progress(task_id: str):
    result = dm_service.get_task_progress(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.post("/api/downloads/{task_id}/pause", response_model=TaskActionResponse)
async def pause_download(task_id: str):
    return dm_service.pause_download(task_id)


@router.post("/api/downloads/{task_id}/resume", response_model=TaskActionResponse)
async def resume_download(task_id: str):
    return await dm_service.resume_download(task_id)


@router.post("/api/downloads/{task_id}/verify", response_model=VerifyFileResponse)
async def verify_download(task_id: str):
    result = dm_service.verify_file(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.post("/api/downloads/{task_id}/delete", response_model=TaskActionResponse)
async def delete_download(task_id: str, request: DeleteDownloadRequest):
    return await dm_service.delete_download(task_id, delete_file=request.delete_file)

@router.get("/api/downloads/browse")
async def browse_folders(path: Optional[str] = None):
    return dm_service.browse_folders(path)


# ===========================================================================
# Configs
# ===========================================================================

@router.get("/api/configs/list", response_model=ListConfigsResponse)
async def list_configs():
    return config_service.list_configs()


@router.get("/api/configs/load/{config_name}", response_model=LoadConfigResponse)
async def load_config(config_name: str):
    return config_service.load_config(config_name)


@router.post("/api/configs/save-from-file", response_model=SaveConfigResponse)
async def save_config_from_file(
        file: UploadFile = File(...),
        config_name: Optional[str] = None,
):
    content = await file.read()
    try:
        config_data = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return SaveConfigResponse(success=False, error=f"Invalid file: {exc}")

    name = config_name or (
        file.filename.rsplit(".", 1)[0] if file.filename and "." in file.filename else file.filename
    )
    return config_service.save_config(name or "config", config_data)


@router.post("/api/configs/delete/{config_name}", response_model=DeleteConfigResponse)
async def delete_config(config_name: str):
    return config_service.delete_config(config_name)
