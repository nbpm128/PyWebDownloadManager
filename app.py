from fastapi import FastAPI

from routers.dashboard import router as dashboard_router
from routers.download_manager import router as download_manager_router
from routers.files_router import router as files_router
from logger import setup_logger
from settings import settings


app = FastAPI(
    title="PyWebDownloadManager",
    description="A web-based asynchronous download manager built with FastAPI and PyDownLib.",
    version="0.1.0",
)


app.include_router(dashboard_router)
app.include_router(files_router)
app.include_router(download_manager_router)


if __name__ == "__main__":
    import uvicorn

    setup_logger(
        level=settings.log_level.upper(),
        log_dir=settings.log_dir,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )

    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
