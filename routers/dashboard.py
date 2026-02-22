import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from services.dashboard_service import DashboardService, ShutdownScheduler

router = APIRouter()

# Configure templates
templates_dir = os.path.join(os.path.dirname(__file__), '..', 'templates')
templates = Jinja2Templates(directory=templates_dir)


# ─────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────

class ShutdownRequest(BaseModel):
    hours: int = 0
    minutes: int = 0
    seconds: int = 0


# ─────────────────────────────────────────────────────────
# PAGE ROUTES
# ─────────────────────────────────────────────────────────

@router.get("/")
async def root():
    """Redirect from home page to dashboard"""
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard")
async def dashboard(request: Request):
    """Dashboard page with system information"""
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "active_page": "dashboard"}
    )


# ─────────────────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────────────────

@router.get("/api/system/info")
async def get_system_info():
    """All system information (memory, disk, cpu, gpu, environment, shutdown status)"""
    return DashboardService.get_all_system_info()


@router.get("/api/system/environment")
async def get_environment():
    """Python / CUDA / PyTorch versions and hostname"""
    return DashboardService.get_environment_info()


@router.post("/api/shutdown/schedule")
async def schedule_shutdown(req: ShutdownRequest):
    """Schedule an automatic shutdown"""
    total_seconds = req.hours * 3600 + req.minutes * 60 + req.seconds
    if total_seconds < 1:
        raise HTTPException(status_code=400, detail="Total time must be at least 1 second")
    ShutdownScheduler.schedule(total_seconds)
    return {"status": "scheduled", "total_seconds": total_seconds}


@router.post("/api/shutdown/cancel")
async def cancel_shutdown():
    """Cancel the scheduled shutdown"""
    ShutdownScheduler.cancel()
    return {"status": "cancelled"}


@router.get("/api/shutdown/status")
async def shutdown_status():
    """Current shutdown scheduler status"""
    return ShutdownScheduler.status()