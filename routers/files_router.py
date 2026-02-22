import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from schemas import FilesResponse, FileMetadataResponse
from services.files_service import FilesService


router = APIRouter(tags=["files"])

_svc = FilesService()

templates_dir = os.path.join(os.path.dirname(__file__), '..', 'templates')
templates = Jinja2Templates(directory=templates_dir)


@router.get("/export")
async def export_file_page(request: Request):
    return templates.TemplateResponse(
        "export_file.html", {"request": request, "active_page": "export"}
    )


@router.get("/api/files/browse", response_model=FilesResponse)
def files_browse(path: Optional[str] = Query(default=None)):
    data = _svc.browse(path)
    if data.get("error"):
        raise HTTPException(status_code=400, detail=data["error"])
    return data


@router.get("/api/files/preview")
def files_preview(path: str = Query(...)):
    try:
        result = _svc.get_preview(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={"Cache-Control": f"max-age={result.cache_max_age}"},
    )


@router.get("/api/files/export-zip")
async def files_export_zip(path: Optional[str] = Query(default=None)):
    try:
        data, filename = await _svc.build_zip_async(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/files/meta", response_model=FileMetadataResponse)
def files_meta(path: str = Query(...)):
    """
    Return metadata for an image or video file.
    - PNG: ComfyUI workflow, prompt JSON, dimensions
    - MP4/MOV/WEBM: dimensions, duration, fps, codec, ComfyUI prompt from Comment tag
    """
    meta = _svc.get_file_meta(path)
    if meta.error:
        raise HTTPException(status_code=404, detail=meta.error)
    return FileMetadataResponse(
        width=meta.width,
        height=meta.height,
        workflow=meta.workflow,
        prompt=meta.prompt,
        raw_text_chunks=meta.raw_text_chunks,
        duration=meta.duration,
        fps=meta.fps,
        video_codec=meta.video_codec,
        comment=meta.comment,
    )