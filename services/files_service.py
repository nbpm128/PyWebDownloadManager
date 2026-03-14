from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from settings import settings

logger = logging.getLogger(__name__)

PREVIEW_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".bmp", ".svg", ".ico", ".tiff", ".avif",
    ".mp4", ".webm", ".ogg", ".mov",
})

VIDEO_EXTS: frozenset[str] = frozenset({
    ".mp4", ".webm", ".ogg", ".mov", ".mkv", ".avi",
})


# ── DTOs ──────────────────────────────────────────────────────────────────────

@dataclass
class PreviewResult:
    content: bytes
    media_type: str
    cache_max_age: int = 3600
    file_path: Optional[Path] = None   # set for video — lets router use FileResponse + Range support


@dataclass
class FileMetadata:
    """Metadata extracted from an image or video file."""
    # Common
    width: Optional[int] = None
    height: Optional[int] = None
    # Image (PNG ComfyUI)
    workflow: Optional[dict] = None  # ComfyUI workflow JSON
    prompt: Optional[dict] = None   # ComfyUI prompt JSON
    raw_text_chunks: dict[str, str] = field(default_factory=dict)
    # Video
    duration: Optional[float] = None  # seconds
    fps: Optional[float] = None
    video_codec: Optional[str] = None
    comment: Optional[str] = None  # raw Comment tag
    # Shared
    error: Optional[str] = None


# ── ZIP job tracking ───────────────────────────────────────────────────────────

ZIP_OUTPUT_DIR = Path(tempfile.gettempdir()) / "zips"
ZIP_TTL_SECONDS = 30 * 60   # remove finished archives after 30 min


class ZipStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    ERROR    = "error"


@dataclass
class ZipJob:
    job_id:    str
    path:      Optional[str]        # relative workdir path requested
    status:    ZipStatus = ZipStatus.PENDING
    progress:  int       = 0        # 0-100
    message:   str       = ""
    filename:  str       = ""
    file_path: Optional[Path] = None
    error:     Optional[str] = None
    created_at: float    = field(default_factory=time.time)
    done_at:   Optional[float] = None


# ── Service ───────────────────────────────────────────────────────────────────

class FilesService:
    """
    Scoped file-system operations for the download manager workdir.

    Public API
    ----------
    browse()            – directory listing (folders + files)
    get_safe_path()     – path validation / traversal guard
    get_preview()       – serve a previewable file as raw bytes
    build_zip()         – build an in-memory ZIP of a directory (legacy)
    build_zip_async()   – async wrapper for use in FastAPI endpoints (legacy)
    create_zip_job()    – enqueue a background ZIP job, returns ZipJob
    get_zip_job()       – look up a job by id
    get_zip_file_path() – return the output path for a completed job
    get_file_meta()     – extract image dimensions + embedded ComfyUI workflow
    """

    def __init__(self) -> None:
        self._jobs: dict[str, ZipJob] = {}
        self._jobs_lock = threading.Lock()
        ZIP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._restore_jobs_from_disk()

    def _job_meta_path(self, job_id: str) -> Path:
        """Sidecar JSON file that persists a finished job across restarts."""
        return ZIP_OUTPUT_DIR / f"{job_id}.json"

    def _save_job_to_disk(self, job: ZipJob) -> None:
        """Write a completed job's metadata to a sidecar JSON file."""
        try:
            data = {
                "job_id":     job.job_id,
                "path":       job.path,
                "status":     job.status.value,
                "filename":   job.filename,
                "file_path":  str(job.file_path) if job.file_path else None,
                "created_at": job.created_at,
                "done_at":    job.done_at,
            }
            self._job_meta_path(job.job_id).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.warning("Failed to persist job metadata | job_id=%s", job.job_id)

    def _restore_jobs_from_disk(self) -> None:
        """
        On startup, scan ZIP_OUTPUT_DIR for sidecar JSON files and rebuild
        the in-memory job registry for all archives that still exist on disk.
        Expired or missing archives are silently skipped.
        """
        now = time.time()
        restored = 0
        for meta_file in ZIP_OUTPUT_DIR.glob("*.json"):
            try:
                data      = json.loads(meta_file.read_text(encoding="utf-8"))
                done_at   = data.get("done_at") or 0
                file_path = Path(data["file_path"]) if data.get("file_path") else None

                # Skip if TTL expired or archive file is gone
                if (now - done_at) > ZIP_TTL_SECONDS:
                    meta_file.unlink(missing_ok=True)
                    if file_path and file_path.exists():
                        file_path.unlink(missing_ok=True)
                    continue
                if not file_path or not file_path.exists():
                    meta_file.unlink(missing_ok=True)
                    continue

                job = ZipJob(
                    job_id     = data["job_id"],
                    path       = data.get("path"),
                    status     = ZipStatus.DONE,
                    progress   = 100,
                    message    = "Done",
                    filename   = data["filename"],
                    file_path  = file_path,
                    created_at = data.get("created_at", done_at),
                    done_at    = done_at,
                )
                self._jobs[job.job_id] = job
                restored += 1
            except Exception:
                logger.debug("Skipping corrupt job metadata | file=%s", meta_file)

        if restored:
            logger.info("Restored %d ZIP job(s) from disk", restored)

    # ── Internal ──────────────────────────────────────────────────────────────

    @property
    def _workdir(self) -> Path:
        return Path(settings.output_path).resolve()

    # ── Browse ────────────────────────────────────────────────────────────────

    def browse(self, relative_path: Optional[str] = None) -> dict:
        """Return a directory listing relative to workdir."""
        base = self._workdir
        current = (base / relative_path).resolve() if relative_path else base

        if not str(current).startswith(str(base)):
            logger.warning(
                "Path traversal attempt in browse | input=%s | resolved=%s | base=%s",
                relative_path, current, base,
            )
            return {"error": "Access denied", "folders": [], "files": [], "current": "", "parts": [],
                    "can_go_up": False}

        try:
            entries = list(current.iterdir())
            folders = sorted(e.name for e in entries if e.is_dir() and not e.name.startswith("."))
            files = sorted(e.name for e in entries if e.is_file() and not e.name.startswith("."))
            rel = current.relative_to(base)
            parts = list(rel.parts) if str(rel) != "." else []
            logger.debug(
                "Directory listed | path=%s | folders=%d | files=%d",
                relative_path or "/", len(folders), len(files),
            )
            return {
                "current": str(rel).replace("\\", "/") if str(rel) != "." else "",
                "parts": parts,
                "folders": folders,
                "files": files,
                "can_go_up": str(rel) != ".",
            }
        except Exception as exc:
            logger.error("Error listing directory | path=%s | error=%s", relative_path, exc)
            return {"error": str(exc), "folders": [], "files": [], "current": "", "parts": [], "can_go_up": False}

    # ── Path safety ───────────────────────────────────────────────────────────

    def get_safe_path(self, relative_path: str) -> Optional[Path]:
        """Resolve *relative_path* inside workdir; return None on traversal."""
        target = (self._workdir / relative_path).resolve()
        if not str(target).startswith(str(self._workdir)):
            logger.warning(
                "Path traversal attempt in get_safe_path | input=%s | resolved=%s | base=%s",
                relative_path, target, self._workdir,
            )
            return None
        return target

    # ── Preview ───────────────────────────────────────────────────────────────

    def get_preview(self, path: str) -> PreviewResult:
        """
        Read and return a file for preview or download.

        For video extensions a ``PreviewResult`` with ``file_path`` set is returned
        so the router can use ``FileResponse`` which supports HTTP Range requests
        (206 Partial Content).  Browsers require Range support to decode the first
        frame when using a hidden ``<video>`` element for canvas thumbnail capture.

        For image/other previewable extensions the file is read into memory and
        served directly (browser renders it).

        For any other extension the file is served as ``application/octet-stream``
        so the browser will offer a download.

        Raises
        ------
        FileNotFoundError – path does not resolve to an existing file.
        """
        target = self.get_safe_path(path)
        if not target or not target.is_file():
            logger.warning("Preview requested for non-existent file | path=%s", path)
            raise FileNotFoundError(f"File not found: {path!r}")

        media_type, _ = mimetypes.guess_type(str(target))

        if target.suffix.lower() not in PREVIEW_EXTS:
            logger.debug(
                "Serving binary download for unsupported extension | path=%s | ext=%s",
                path, target.suffix,
            )
            return PreviewResult(
                content=target.read_bytes(),
                media_type=media_type or "application/octet-stream",
                cache_max_age=0,
            )

        # Video: return the file path so the router can use FileResponse.
        # FileResponse uses Starlette's StaticFiles Range-request machinery,
        # which correctly responds with 206 Partial Content — required for
        # browsers to seek into the video and capture the first frame.
        if target.suffix.lower() in VIDEO_EXTS:
            logger.debug("Serving video via FileResponse (Range-capable) | path=%s", path)
            return PreviewResult(
                content=b"",                              # not used by router when file_path is set
                media_type=media_type or "video/mp4",
                file_path=target,
            )

        logger.debug("Serving preview | path=%s | media_type=%s", path, media_type)
        return PreviewResult(
            content=target.read_bytes(),
            media_type=media_type or "application/octet-stream",
        )

    # ── ZIP export (background job) ───────────────────────────────────────────

    def create_zip_job(self, relative_path: Optional[str] = None) -> ZipJob:
        """
        Enqueue a background ZIP job and return a ZipJob immediately.
        The actual archiving runs in a daemon thread so the HTTP response
        is returned to the client right away.
        """
        self._purge_old_jobs()

        job_id = uuid.uuid4().hex
        job = ZipJob(job_id=job_id, path=relative_path)

        with self._jobs_lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_zip_job,
            args=(job,),
            daemon=True,
            name=f"zip-{job_id[:8]}",
        )
        thread.start()
        logger.info("ZIP job enqueued | job_id=%s | path=%s", job_id, relative_path)
        return job

    def get_zip_job(self, job_id: str) -> Optional[ZipJob]:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def get_zip_file_path(self, job_id: str) -> Optional[Path]:
        """Return the output file path if the job is done, else None."""
        job = self.get_zip_job(job_id)
        if job and job.status == ZipStatus.DONE and job.file_path and job.file_path.exists():
            return job.file_path
        return None

    def list_zip_jobs(self) -> list[ZipJob]:
        """Return all known jobs, newest first."""
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def delete_zip_job(self, job_id: str) -> bool:
        """Delete a ZIP job and its archive file. Returns True if found and deleted."""
        with self._jobs_lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        if job.file_path and job.file_path.exists():
            try:
                job.file_path.unlink()
            except OSError:
                pass
        self._job_meta_path(job_id).unlink(missing_ok=True)
        logger.info("ZIP job deleted by user | job_id=%s", job_id)
        return True

    def _run_zip_job(self, job: ZipJob) -> None:
        """Worker executed in a background thread."""
        try:
            job.status   = ZipStatus.RUNNING
            job.message  = "Scanning files…"
            job.progress = 0

            base   = self._workdir
            target = (base / job.path).resolve() if job.path else base

            if not str(target).startswith(str(base)):
                raise PermissionError("Access denied")
            if not target.is_dir():
                raise ValueError("Path is not a directory")

            # Collect all files first so we can report accurate progress
            files = sorted(
                fp for fp in target.rglob("*")
                if fp.is_file() and not fp.name.startswith(".")
            )
            total        = len(files)
            job.message  = f"Archiving {total} file{'s' if total != 1 else ''}…"

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            zip_name  = f"{target.name or 'export'}_{timestamp}.zip"
            zip_path  = ZIP_OUTPUT_DIR / zip_name

            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i, fp in enumerate(files, 1):
                    zf.write(fp, fp.relative_to(target))
                    job.progress = int(i / max(total, 1) * 95)  # reserve last 5% for flush

            job.file_path = zip_path
            job.filename  = zip_name
            job.progress  = 100
            job.status    = ZipStatus.DONE
            job.done_at   = time.time()
            job.message   = "Done"
            self._save_job_to_disk(job)
            logger.info(
                "ZIP job finished | job_id=%s | filename=%s | size=%d",
                job.job_id, zip_name, zip_path.stat().st_size,
            )

        except Exception as exc:
            job.status  = ZipStatus.ERROR
            job.error   = str(exc)
            job.message = f"Error: {exc}"
            logger.exception("ZIP job failed | job_id=%s", job.job_id)

    def _purge_old_jobs(self) -> None:
        """Remove finished jobs and their archive files older than ZIP_TTL_SECONDS."""
        now = time.time()
        with self._jobs_lock:
            stale = [
                jid for jid, job in self._jobs.items()
                if job.done_at and (now - job.done_at) > ZIP_TTL_SECONDS
            ]
            for jid in stale:
                job = self._jobs.pop(jid)
                if job.file_path and job.file_path.exists():
                    try:
                        job.file_path.unlink()
                    except OSError:
                        pass
                self._job_meta_path(jid).unlink(missing_ok=True)
                logger.debug("ZIP job purged | job_id=%s", jid)

    # ── ZIP export (legacy in-memory, kept for compatibility) ─────────────────

    def build_zip(self, relative_path: Optional[str] = None) -> tuple[bytes, str]:
        """Build an in-memory ZIP of a directory (legacy, kept for compatibility)."""
        base   = self._workdir
        target = (base / relative_path).resolve() if relative_path else base

        if not str(target).startswith(str(base)):
            raise PermissionError("Access denied")
        if not target.is_dir():
            raise ValueError("Path is not a directory")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(target.rglob("*")):
                if fp.is_file() and not fp.name.startswith("."):
                    zf.write(fp, fp.relative_to(target))
        buf.seek(0)
        data     = buf.read()
        filename = f"{target.name or 'export'}.zip"
        logger.info("ZIP built (legacy) | path=%s | size=%d", relative_path or "/", len(data))
        return data, filename

    async def build_zip_async(self, relative_path: Optional[str] = None) -> tuple[bytes, str]:
        """Async wrapper around build_zip (legacy)."""
        return await asyncio.to_thread(self.build_zip, relative_path)

    # ── Metadata (images + video) ─────────────────────────────────────────────

    def get_file_meta(self, path: str) -> FileMetadata:
        """
        Extract metadata from an image or video file.

        • PNG  → parse binary chunks, lift ComfyUI workflow/prompt JSON
        • MP4 / MOV / WEBM / OGG → run ffprobe, parse Comment tag for
          ComfyUI prompt JSON, extract dimensions/duration/fps
        • Other images → Pillow fallback (dimensions only)

        Never raises – errors land in ``FileMetadata.error``.
        """
        target = self.get_safe_path(path)
        if not target or not target.is_file():
            logger.warning("get_file_meta: file not found | path=%s", path)
            return FileMetadata(error=f"File not found: {path!r}")

        ext = target.suffix.lower()
        logger.debug("Extracting file metadata | path=%s | ext=%s", path, ext)

        if ext == ".png":
            return self._parse_png(target)

        if ext in VIDEO_EXTS:
            return self._parse_video(target)

        return self._parse_generic_image(target)

    # Keep old name as alias for backwards compatibility
    def get_image_meta(self, path: str) -> FileMetadata:
        return self.get_file_meta(path)

    # ── PNG chunk parser ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_png(path: Path) -> FileMetadata:
        PNG_SIG = b"\x89PNG\r\n\x1a\n"
        meta = FileMetadata()

        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.error("Failed to read PNG file | path=%s | error=%s", path, exc)
            meta.error = str(exc)
            return meta

        if not data.startswith(PNG_SIG):
            logger.warning("Invalid PNG signature | path=%s", path)
            meta.error = "Not a valid PNG file"
            return meta

        offset = 8  # skip 8-byte signature
        while offset + 8 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk_type = data[offset + 4:offset + 8].decode("ascii", errors="replace")
            chunk_data = data[offset + 8:offset + 8 + length]
            offset += 12 + length  # 4 len + 4 type + data + 4 crc

            # ── IHDR: image dimensions ────────────────────────────────────────
            if chunk_type == "IHDR" and len(chunk_data) >= 8:
                meta.width, meta.height = struct.unpack(">II", chunk_data[:8])

            # ── tEXt: uncompressed Latin-1 text ──────────────────────────────
            elif chunk_type == "tEXt":
                nul = chunk_data.find(b"\x00")
                if nul != -1:
                    key = chunk_data[:nul].decode("latin-1")
                    value = chunk_data[nul + 1:].decode("latin-1", errors="replace")
                    meta.raw_text_chunks[key] = value

            # ── zTXt: zlib-compressed text ────────────────────────────────────
            elif chunk_type == "zTXt":
                nul = chunk_data.find(b"\x00")
                if nul != -1 and len(chunk_data) > nul + 2:
                    key = chunk_data[:nul].decode("latin-1")
                    try:
                        value = zlib.decompress(chunk_data[nul + 2:]).decode("latin-1")
                        meta.raw_text_chunks[key] = value
                    except Exception:
                        pass

            # ── iTXt: international UTF-8 text ───────────────────────────────
            elif chunk_type == "iTXt":
                nul = chunk_data.find(b"\x00")
                if nul != -1 and len(chunk_data) > nul + 3:
                    key = chunk_data[:nul].decode("utf-8", errors="replace")
                    compression_flag = chunk_data[nul + 1]
                    rest = chunk_data[nul + 3:]  # skip flag + method byte
                    nul2 = rest.find(b"\x00")
                    if nul2 != -1:
                        rest = rest[nul2 + 1:]
                    nul3 = rest.find(b"\x00")
                    if nul3 != -1:
                        rest = rest[nul3 + 1:]
                    try:
                        text = (
                            zlib.decompress(rest).decode("utf-8")
                            if compression_flag
                            else rest.decode("utf-8", errors="replace")
                        )
                        meta.raw_text_chunks[key] = text
                    except Exception:
                        pass

            elif chunk_type == "IEND":
                break

        # ── Lift ComfyUI-specific chunks ──────────────────────────────────────
        for candidate in ("workflow", "Workflow"):
            if candidate in meta.raw_text_chunks:
                meta.workflow = FilesService._try_json(meta.raw_text_chunks[candidate])
                break

        for candidate in ("prompt", "Prompt"):
            if candidate in meta.raw_text_chunks:
                meta.prompt = FilesService._try_json(meta.raw_text_chunks[candidate])
                break

        logger.debug(
            "PNG metadata parsed | path=%s | size=%sx%s | has_workflow=%s | has_prompt=%s",
            path, meta.width, meta.height,
            meta.workflow is not None, meta.prompt is not None,
        )
        return meta

    # ── Video parser (ffprobe) ────────────────────────────────────────────────

    @staticmethod
    def _parse_video(path: Path) -> FileMetadata:
        """Extract metadata from a video file via ffprobe."""
        meta = FileMetadata()
        logger.debug("Parsing video metadata via ffprobe | path=%s", path)

        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams",
                    "-show_format",
                    str(path),
                ],
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError:
            logger.warning("ffprobe not found; install ffmpeg to read video metadata | path=%s", path)
            meta.error = "ffprobe not found; install ffmpeg to read video metadata"
            return meta
        except subprocess.TimeoutExpired:
            logger.warning("ffprobe timed out | path=%s", path)
            meta.error = "ffprobe timed out"
            return meta
        except Exception as exc:
            logger.error("ffprobe subprocess error | path=%s | error=%s", path, exc)
            meta.error = str(exc)
            return meta

        try:
            data = json.loads(result.stdout)
        except Exception:
            logger.error("Failed to parse ffprobe JSON output | path=%s", path)
            meta.error = "Failed to parse ffprobe output"
            return meta

        # ── Video stream ──────────────────────────────────────────────────────
        for stream in data.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            meta.width = stream.get("width")
            meta.height = stream.get("height")
            meta.video_codec = stream.get("codec_name")

            fps_str = stream.get("r_frame_rate") or stream.get("avg_frame_rate", "")
            if "/" in fps_str:
                num, den = fps_str.split("/", 1)
                try:
                    meta.fps = round(int(num) / int(den), 3) if int(den) else None
                except (ValueError, ZeroDivisionError):
                    pass
            break

        # ── Format / container tags ───────────────────────────────────────────
        fmt = data.get("format", {})

        try:
            meta.duration = float(fmt.get("duration", 0)) or None
        except (TypeError, ValueError):
            pass

        tags = fmt.get("tags", {})

        # ── Comment tag (legacy VHS / older ComfyUI exporters) ────────────────
        # Some exporters pack workflow+prompt together inside a single Comment tag
        # as a JSON object: {"nodes": [...]} or {"prompt": {...}, "workflow": {...}}
        comment_raw = (
            tags.get("comment")
            or tags.get("Comment")
            or tags.get("COMMENT")
        )
        if comment_raw:
            meta.comment = comment_raw
            parsed = FilesService._try_json(comment_raw)
            if isinstance(parsed, dict):
                if "nodes" in parsed:
                    meta.workflow = parsed
                    if "prompt" in parsed and isinstance(parsed["prompt"], dict):
                        meta.prompt = parsed["prompt"]
                elif "prompt" in parsed:
                    meta.prompt = parsed["prompt"]
                    if "workflow" in parsed and isinstance(parsed["workflow"], dict):
                        meta.workflow = parsed["workflow"]
                else:
                    meta.prompt = parsed

        # ── Standalone workflow / prompt tags  ──────────
        # MediaInfo shows these as top-level tags alongside comment:
        #   workflow: {"id": "...", "nodes": [...], ...}
        #   prompt:   {"10": {"class_type": "...", "inputs": {...}}, ...}
        # Only fill fields not already populated by the comment block above.
        if meta.workflow is None:
            for key in ("workflow", "Workflow", "WORKFLOW"):
                raw = tags.get(key)
                if raw:
                    parsed = FilesService._try_json(raw)
                    if isinstance(parsed, dict) and "nodes" in parsed:
                        meta.workflow = parsed
                        if meta.comment is None:
                            meta.comment = raw
                    break

        if meta.prompt is None:
            for key in ("prompt", "Prompt", "PROMPT"):
                raw = tags.get(key)
                if raw:
                    parsed = FilesService._try_json(raw)
                    if isinstance(parsed, dict):
                        meta.prompt = parsed
                    break

        logger.debug(
            "Video metadata parsed | path=%s | size=%sx%s | duration=%s | fps=%s | codec=%s",
            path, meta.width, meta.height, meta.duration, meta.fps, meta.video_codec,
        )
        return meta

    @staticmethod
    def _parse_generic_image(path: Path) -> FileMetadata:
        try:
            from PIL import Image
            with Image.open(path) as img:
                meta = FileMetadata(width=img.width, height=img.height)
                logger.debug("Generic image metadata parsed | path=%s | size=%sx%s", path, img.width, img.height)
                return meta
        except ImportError:
            logger.warning("Pillow not installed; cannot read non-PNG image metadata | path=%s", path)
            return FileMetadata(error="Pillow not installed; cannot read non-PNG metadata")
        except Exception as exc:
            logger.error("Failed to parse generic image metadata | path=%s | error=%s", path, exc)
            return FileMetadata(error=str(exc))

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _try_json(text: str) -> Optional[Any]:
        try:
            return json.loads(text)
        except Exception:
            return None