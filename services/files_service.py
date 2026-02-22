from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import struct
import subprocess
import zipfile
import zlib
from dataclasses import dataclass, field
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


# ── Service ───────────────────────────────────────────────────────────────────

class FilesService:
    """
    Scoped file-system operations for the download manager workdir.

    Public API
    ----------
    browse()          – directory listing (folders + files)
    get_safe_path()   – path validation / traversal guard
    get_preview()     – serve a previewable file as raw bytes
    build_zip()       – build an in-memory ZIP of a directory
    build_zip_async() – async wrapper for use in FastAPI endpoints
    get_file_meta()   – extract image dimensions + embedded ComfyUI workflow
    """

    def __init__(self) -> None:
        pass

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
        Read and return a previewable file.

        Raises
        ------
        FileNotFoundError – path does not resolve to an existing file.
        PermissionError   – extension not in PREVIEW_EXTS.
        """
        target = self.get_safe_path(path)
        if not target or not target.is_file():
            logger.warning("Preview requested for non-existent file | path=%s", path)
            raise FileNotFoundError(f"File not found: {path!r}")
        if target.suffix.lower() not in PREVIEW_EXTS:
            logger.warning(
                "Preview not supported for extension | path=%s | ext=%s",
                path, target.suffix,
            )
            raise PermissionError(f"Preview not supported for extension {target.suffix!r}")

        media_type, _ = mimetypes.guess_type(str(target))
        logger.debug("Serving preview | path=%s | media_type=%s", path, media_type)
        return PreviewResult(
            content=target.read_bytes(),
            media_type=media_type or "application/octet-stream",
        )

    # ── ZIP export ────────────────────────────────────────────────────────────

    def build_zip(self, relative_path: Optional[str] = None) -> tuple[bytes, str]:
        """
        Build an in-memory ZIP of a directory.

        Returns
        -------
        (zip_bytes, suggested_filename)

        Raises
        ------
        PermissionError – path escapes workdir.
        ValueError      – path is not a directory.
        """
        base = self._workdir
        target = (base / relative_path).resolve() if relative_path else base

        if not str(target).startswith(str(base)):
            logger.warning(
                "Path traversal attempt in build_zip | input=%s | resolved=%s | base=%s",
                relative_path, target, base,
            )
            raise PermissionError("Access denied")
        if not target.is_dir():
            logger.warning("build_zip called on non-directory | path=%s", relative_path)
            raise ValueError("Path is not a directory")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(target.rglob("*")):
                if fp.is_file() and not fp.name.startswith("."):
                    zf.write(fp, fp.relative_to(target))
        buf.seek(0)
        data = buf.read()
        filename = f"{target.name or 'export'}.zip"
        logger.info(
            "ZIP archive built | path=%s | filename=%s | size_bytes=%d",
            relative_path or "/", filename, len(data),
        )
        return data, filename

    async def build_zip_async(self, relative_path: Optional[str] = None) -> tuple[bytes, str]:
        """Async wrapper around build_zip."""
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