from __future__ import annotations

import asyncio
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydownlib import (
    DownloadManager,
    DownloadTask,
    DownloadStatus,
    MirrorUrl,
    ExtractOptions,
    ArchiveFormat
)

from schemas.downloads import (
    AddDownloadRequest,
    AddDownloadResponse,
    AllTasksResponse,
    TaskActionResponse,
    TaskProgressResponse,
    TaskSchema,
    VerifyFileResponse,
    MirrorSchema,
    SetConcurrencyResponse,
    GetConcurrencyResponse,
    ExtractFileResponse,
)
from settings import settings

logger = logging.getLogger(__name__)


class DownloadManagerService:
    """Singleton service for managing file downloads via DownloadManager."""

    _instance: Optional[DownloadManagerService] = None
    _download_manager: Optional[DownloadManager] = None

    def __new__(cls) -> DownloadManagerService:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        os.makedirs(settings.workspace_path, exist_ok=True)

        self._download_manager = DownloadManager(
            max_concurrent=settings.max_concurrent,
            default_download_path=settings.workspace_path
        )
        self._initialized = True
        logger.info(
            "DownloadManagerService initialised | workspace=%s | max_concurrent=%s",
            settings.workspace_path,
            settings.max_concurrent,
        )

    # ------------------------------------------------------------------
    # Task registration
    # ------------------------------------------------------------------

    async def add_download(self, request: AddDownloadRequest) -> AddDownloadResponse:
        """Add a new download task."""
        mirrors = request.mirrors
        if not mirrors:
            logger.warning("add_download called with no mirrors | file_name=%s", request.file_name)
            return AddDownloadResponse(success=False, error="No mirrors provided")

        try:
            extract_opts = None
            if request.extract:
                extract_opts = ExtractOptions(
                    format=ArchiveFormat(request.extract.format),
                    destination=request.extract.destination,
                    remove_archive=request.extract.remove_archive,
                    password=request.extract.password,
                )

            download_task = DownloadTask(
                file_name=request.file_name,
                folder_path=request.folder_path,
                mirrors=self._normalise_mirrors(mirrors),
                expected_hash=request.expected_hash,
                hash_algorithm=request.hash_algorithm,
                extract=extract_opts
            )

            await self._download_manager.add_task(download_task)

            logger.info(
                "Download task added | task_id=%s | file_name=%s | mirrors=%d",
                download_task.task_id,
                request.file_name,
                len(mirrors),
            )
            return AddDownloadResponse(
                success=True,
                task_id=download_task.task_id,
                message="Download task added successfully",
            )
        except Exception as exc:
            logger.error(
                "Failed to add download task | file_name=%s | error=%s",
                request.file_name, exc,
            )
            return AddDownloadResponse(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_downloads(self) -> TaskActionResponse:
        logger.info("Starting download queue")
        try:
            await self._download_manager.start_downloads()
            logger.info("Download queue started successfully")
            return TaskActionResponse(success=True, message="Downloads started")
        except Exception as exc:
            logger.error("Failed to start download queue | error=%s", exc)
            return TaskActionResponse(success=False, message=str(exc))

    async def stop_all_downloads(self) -> TaskActionResponse:
        logger.info("Stopping all downloads")
        try:
            await self._download_manager.stop_downloads()
            count = len(self._download_manager.tasks)
            logger.info("All downloads stopped | task_count=%d", count)
            return TaskActionResponse(success=True, message=f"Stopped {count} download(s)")
        except Exception as exc:
            logger.error("Failed to stop downloads | error=%s", exc)
            return TaskActionResponse(success=False, message=str(exc))

    def pause_download(self, task_id: str) -> TaskActionResponse:
        success = self._download_manager.stop_download(task_id)
        if success:
            logger.info("Download paused | task_id=%s", task_id)
        else:
            logger.warning("Failed to pause download | task_id=%s", task_id)
        return TaskActionResponse(
            success=success,
            message="Download paused" if success else "Failed to pause download",
        )

    async def resume_download(self, task_id: str) -> TaskActionResponse:
        success = await self._download_manager.resume_download(task_id)
        if success:
            logger.info("Download resumed | task_id=%s", task_id)
        else:
            logger.warning("Failed to resume download | task_id=%s", task_id)
        return TaskActionResponse(
            success=success,
            message="Download resumed" if success else "Failed to resume download",
        )

    async def delete_download(self, task_id: str, delete_file: bool = False) -> TaskActionResponse:
        try:
            if task_id not in self._download_manager.tasks:
                logger.warning("Delete requested for unknown task | task_id=%s", task_id)
                return TaskActionResponse(success=False, message="Task not found")
            task = self._download_manager.tasks[task_id]
            success = await self._download_manager.delete_task(task, delete_file=delete_file)
            if success:
                logger.info(
                    "Download task deleted | task_id=%s | delete_file=%s",
                    task_id, delete_file,
                )
            else:
                logger.warning("Failed to delete download task | task_id=%s", task_id)
            return TaskActionResponse(
                success=success,
                message="Download deleted" if success else "Failed to delete file",
            )
        except Exception as exc:
            logger.error("Error deleting download task | task_id=%s | error=%s", task_id, exc)
            return TaskActionResponse(success=False, message=str(exc))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all_tasks(self) -> AllTasksResponse:
        tasks = [self._task_to_schema(t) for t in self._download_manager.tasks.values()]
        return AllTasksResponse(tasks=tasks)

    def get_task_progress(self, task_id: str) -> Optional[TaskProgressResponse]:
        progress = self._download_manager.get_progress(task_id)
        if not progress:
            return None
        return TaskProgressResponse(
            task_id=task_id,
            url=progress.current_url,
            status=progress.status,
            downloaded_bytes=progress.downloaded_bytes,
            total_bytes=progress.total_bytes,
            progress_percent=progress.percentage or 0.0
        )

    def verify_file(self, task_id: str) -> Optional[VerifyFileResponse]:
        result = self._download_manager.verify_file(task_id)
        if not result:
            return None
        logger.debug(
            "File verification result | task_id=%s | is_valid=%s | algorithm=%s",
            task_id, result.verified, result.algorithm,
        )
        return VerifyFileResponse(
            task_id=task_id,
            is_valid=result.verified or False,
            expected_hash=result.expected_hash,
            actual_hash=result.actual_hash,
            algorithm=result.algorithm
        )

    async def set_concurrency(self, max_concurrent: int) -> SetConcurrencyResponse:
        if max_concurrent < 1:
            logger.warning("Invalid concurrency value | max_concurrent=%d", max_concurrent)
            return SetConcurrencyResponse(success=False, error="max_concurrent must be >= 1")
        try:
            await self._download_manager.queue_manager.set_concurrency(max_concurrent)
            logger.info("Concurrency updated | max_concurrent=%d", max_concurrent)
            return SetConcurrencyResponse(
                success=True,
                max_concurrent=max_concurrent,
                message=f"Concurrency set to {max_concurrent}",
            )
        except Exception as exc:
            logger.error("Failed to set concurrency | max_concurrent=%d | error=%s", max_concurrent, exc)
            return SetConcurrencyResponse(success=False, error=str(exc))

    def get_concurrency(self) -> GetConcurrencyResponse:
        try:
            value = self._download_manager.queue_manager.get_max_concurrent()
            return GetConcurrencyResponse(success=True, max_concurrent=value)
        except Exception as exc:
            logger.error("Failed to get concurrency | error=%s", exc)
            return GetConcurrencyResponse(success=False, error=str(exc))

    def browse_folders(self, relative_path: Optional[str] = None) -> dict:
        base = Path(self._download_manager._default_download_path)
        current = (base / relative_path) if relative_path else base
        current = current.resolve()

        if not str(current).startswith(str(base.resolve())):
            logger.warning(
                "Path traversal attempt in browse_folders | input=%s | resolved=%s | base=%s",
                relative_path, current, base,
            )
            return {"error": "Access denied", "folders": [], "current": ""}

        try:
            folders = sorted([
                f.name for f in current.iterdir()
                if f.is_dir() and not f.name.startswith('.')
            ])
            rel = current.relative_to(base.resolve())
            parts = list(rel.parts) if str(rel) != '.' else []
            return {
                "current": str(rel).replace('\\', '/') if str(rel) != '.' else "",
                "parts": parts,
                "folders": folders,
                "can_go_up": str(rel) != '.',
            }
        except Exception as e:
            logger.error("Error browsing folders | path=%s | error=%s", relative_path, e)
            return {"error": str(e), "folders": [], "current": ""}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_mirrors(mirrors: list[MirrorSchema]) -> list[MirrorUrl]:
        """Ensure every mirror has a unique priority value."""
        explicit = {m.priority for m in mirrors if m.priority is not None}
        next_auto = 1
        result = []
        for mirror in mirrors:
            m = mirror.model_dump()
            if m["priority"] is None:
                while next_auto in explicit:
                    next_auto += 1
                m["priority"] = next_auto
                explicit.add(next_auto)
                next_auto += 1

            mirror_url = MirrorUrl.from_dict(m)
            result.append(mirror_url)

        logger.debug(
            "Mirrors normalised | count=%d | priorities=%s",
            len(result),
            [m.priority for m in result],
        )
        return result

    @staticmethod
    def _task_to_schema(task: DownloadTask) -> TaskSchema:
        total = task.total_bytes or 0
        downloaded = task.downloaded_bytes or 0
        progress = round((downloaded / total) * 100, 2) if total > 0 else 0.0

        speed: Optional[float] = None
        if task.created_at and task.status != DownloadStatus.PENDING:
            try:
                created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
                now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
                elapsed = (now - created).total_seconds()
                if elapsed > 0 and downloaded > 0:
                    speed = downloaded / elapsed
            except Exception:
                pass

        return TaskSchema(
            task_id=task.task_id,
            url=task.get_current_mirror().url,
            file_name=task.file_name,
            folder_path=task.folder_path,
            filepath=task.filepath,
            status=task.status,
            downloaded_bytes=downloaded,
            total_bytes=total,
            progress_percent=progress,
            speed=speed,
            created_at=task.created_at,
            completed_at=task.completed_at,
            error_message=task.error_message,
            expected_hash=task.expected_hash,
            mirrors_count=len(task.mirrors),
        )

    async def extract_file(self, task_id: str) -> ExtractFileResponse:
        if task_id not in self._download_manager.tasks:
            logger.warning("extract_file: task not found | task_id=%s", task_id)
            return ExtractFileResponse(success=False, error="Task not found")

        task = self._download_manager.tasks[task_id]

        if task.status != DownloadStatus.COMPLETED:
            logger.warning(
                "extract_file: file not fully downloaded | task_id=%s | status=%s",
                task_id, task.status,
            )
            return ExtractFileResponse(success=False, error="File is not downloaded yet")

        if not task.filepath or not task.filepath.lower().endswith('.zip'):
            logger.warning(
                "extract_file: file is not a ZIP archive | task_id=%s | filepath=%s",
                task_id, task.filepath,
            )
            return ExtractFileResponse(success=False, error="File is not a ZIP archive")

        filepath = Path(task.filepath)
        if not filepath.exists():
            logger.warning(
                "extract_file: file not found on disk | task_id=%s | filepath=%s",
                task_id, filepath,
            )
            return ExtractFileResponse(success=False, error="File not found on disk")

        destination = Path(task.folder_path)

        def _extract():
            with zipfile.ZipFile(filepath, 'r') as zf:
                zf.extractall(destination)

        try:
            await asyncio.to_thread(_extract)
            logger.info(
                "ZIP extracted successfully | task_id=%s | source=%s | destination=%s",
                task_id, filepath, destination,
            )
            return ExtractFileResponse(success=True, message=f"Extracted to {destination}")
        except Exception as exc:
            logger.error(
                "ZIP extraction failed | task_id=%s | source=%s | error=%s",
                task_id, filepath, exc,
            )
            return ExtractFileResponse(success=False, error=str(exc))