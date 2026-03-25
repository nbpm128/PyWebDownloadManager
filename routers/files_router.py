import asyncio
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from schemas import FilesResponse, FileMetadataResponse, ZipJobResponse
from services.files_service import FilesService, ZipStatus

router = APIRouter(tags=["files"])

_svc = FilesService()

templates_dir = os.path.join(os.path.dirname(__file__), '..', 'templates')
templates = Jinja2Templates(directory=templates_dir)


@router.get("/export")
async def export_file_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="export_file.html", context={"request": request, "active_page": "export"}
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

    headers = {
        "Cache-Control": f"max-age={result.cache_max_age}",
    }

    if result.file_path is not None:
        return FileResponse(
            path=str(result.file_path),
            media_type=result.media_type,
            headers=headers,
        )

    if result.content is None:
        raise HTTPException(status_code=500, detail="Preview content missing")

    return Response(
        content=result.content,
        media_type=result.media_type,
        headers=headers,
    )



# ── ZIP (background job) ──────────────────────────────────────────────────────

@router.get("/api/files/zip/list")
def zip_list():
    """Return all known ZIP jobs (restored from disk + in-memory), newest first."""
    jobs = _svc.list_zip_jobs()
    return [
        ZipJobResponse(
            job_id   = j.job_id,
            status   = j.status,
            progress = j.progress,
            message  = j.message,
            filename = j.filename,
        )
        for j in jobs
    ]


@router.post("/api/files/zip/create", response_model=ZipJobResponse)
def zip_create(path: Optional[str] = Query(default=None)):
    """Start a background ZIP job and return a job_id immediately."""
    job = _svc.create_zip_job(path)
    return ZipJobResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        filename=job.filename,
    )


@router.get("/api/files/zip/status/{job_id}", response_model=ZipJobResponse)
def zip_status(job_id: str):
    """Poll job progress. Returns status, progress (0–100), and filename when done."""
    job = _svc.get_zip_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ZipJobResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        filename=job.filename,
        error=job.error,
    )


@router.get("/api/files/zip/download/{job_id}")
def zip_download(job_id: str):
    """Download the finished ZIP archive."""
    file_path = _svc.get_zip_file_path(job_id)
    if not file_path:
        job = _svc.get_zip_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status == ZipStatus.ERROR:
            raise HTTPException(status_code=500, detail=job.error or "ZIP failed")
        raise HTTPException(status_code=409, detail="Archive not ready yet")

    job = _svc.get_zip_job(job_id)
    return FileResponse(
        path=file_path,
        media_type="application/zip",
        filename=job.filename,
    )


@router.delete("/api/files/zip/delete/{job_id}", status_code=204)
def zip_delete(job_id: str):
    """Delete a ZIP job and its archive file."""
    found = _svc.delete_zip_job(job_id)
    if not found:
        raise HTTPException(status_code=404, detail="Job not found")



@router.get("/api/files/zip/progress/{job_id}")
async def zip_progress(job_id: str):
    """
    SSE stream that pushes job progress events until the job finishes.

    Events
    ------
    progress   – { progress: int, message: str }
    done       – { progress: 100, message: str, filename: str }
    error_event – { message: str }
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    job = _svc.get_zip_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        POLL_INTERVAL = 0.25   # seconds between status checks

        while True:
            j = _svc.get_zip_job(job_id)
            if j is None:
                # Job disappeared (purged) — close stream
                break

            payload = _json.dumps({"progress": j.progress, "message": j.message})

            if j.status == ZipStatus.DONE:
                done_payload = _json.dumps({
                    "progress": 100,
                    "message":  j.message,
                    "filename": j.filename,
                })
                yield f"event: done\ndata: {done_payload}\n\n"
                break

            elif j.status == ZipStatus.ERROR:
                err_payload = _json.dumps({"message": j.message or "Archive failed"})
                yield f"event: error_event\ndata: {err_payload}\n\n"
                break

            else:
                # PENDING or RUNNING — send progress tick
                yield f"event: progress\ndata: {payload}\n\n"

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


# ── ZIP (legacy streaming, kept for compatibility) ────────────────────────────

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