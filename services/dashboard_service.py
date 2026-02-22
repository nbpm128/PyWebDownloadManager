import logging
import os
import subprocess
import sys
import threading
import time
from typing import Dict, Any, Optional, Tuple, List

import psutil

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# SHUTDOWN SCHEDULER
# ─────────────────────────────────────────────────────────

_shutdown_lock = threading.Lock()
_shutdown_scheduled: bool = False
_shutdown_time: Optional[float] = None
_shutdown_thread: Optional[threading.Thread] = None


def _runpod_shutdown_command() -> Tuple[Optional[List[str]], Optional[str]]:
    """Determine the appropriate RunPod shutdown command."""
    pod_id = os.environ.get("RUNPOD_POD_ID", "").strip()
    if not pod_id:
        return None, None

    mode = os.environ.get("RUNPOD_POD_SHUTDOWN", "").strip().lower()
    if mode in ("remove", "terminate", "delete"):
        return ["runpodctl", "remove", "pod", pod_id], "remove"
    if mode in ("stop", "halt"):
        return ["runpodctl", "stop", "pod", pod_id], "stop"

    if os.environ.get("RUNPOD_NETWORK_VOLUME_ID"):
        return ["runpodctl", "remove", "pod", pod_id], "remove"

    volume_type = os.environ.get("RUNPOD_VOLUME_TYPE", "").strip().lower()
    if volume_type in ("network", "network-volume", "nfs", "volume"):
        return ["runpodctl", "remove", "pod", pod_id], "remove"

    # Default: stop (safer — won't delete local-storage pods)
    return ["runpodctl", "stop", "pod", pod_id], "stop"


def _shutdown_worker() -> None:
    global _shutdown_scheduled, _shutdown_time

    while True:
        with _shutdown_lock:
            if not _shutdown_scheduled or _shutdown_time is None:
                break
            remaining = _shutdown_time - time.time()
            if remaining <= 0:
                cmd, _ = _runpod_shutdown_command()
                try:
                    if cmd:
                        subprocess.run(cmd, check=False)
                    else:
                        os.system("shutdown -h now")
                except Exception:
                    os.system("shutdown -h now")
                break
            sleep_for = min(10, remaining)
            _shutdown_lock.release()
            time.sleep(sleep_for)
            _shutdown_lock.acquire()


class ShutdownScheduler:
    @staticmethod
    def schedule(total_seconds: int) -> None:
        global _shutdown_scheduled, _shutdown_time, _shutdown_thread
        if total_seconds < 1:
            raise ValueError("Delay must be at least 1 second")
        with _shutdown_lock:
            _shutdown_scheduled = True
            _shutdown_time = time.time() + total_seconds
            _shutdown_thread = threading.Thread(target=_shutdown_worker, daemon=True)
            _shutdown_thread.start()

    @staticmethod
    def cancel() -> None:
        global _shutdown_scheduled, _shutdown_time, _shutdown_thread
        with _shutdown_lock:
            _shutdown_scheduled = False
            _shutdown_time = None
            _shutdown_thread = None

    @staticmethod
    def status() -> Dict[str, Any]:
        with _shutdown_lock:
            if not _shutdown_scheduled or _shutdown_time is None:
                return {"scheduled": False}
            remaining = max(0, int(_shutdown_time - time.time()))
            return {
                "scheduled": True,
                "time_remaining": remaining,
                "shutdown_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(_shutdown_time)),
            }


# ─────────────────────────────────────────────────────────
# DASHBOARD SERVICE
# ─────────────────────────────────────────────────────────

class DashboardService:
    """Service for collecting system information"""

    @staticmethod
    def get_memory_info() -> Dict[str, Any]:
        """Get information about RAM"""
        memory = psutil.virtual_memory()
        return {
            "total": memory.total,
            "used": memory.used,
            "free": memory.available,
            "percent": memory.percent,
        }

    @staticmethod
    def get_disk_info() -> Dict[str, Any]:
        """Get disk info for / and /workspace (if present)"""
        def _usage(path: str) -> Optional[Dict[str, Any]]:
            if not os.path.exists(path):
                return None
            try:
                d = psutil.disk_usage(path)
                return {
                    "mount": path,
                    "total": d.total,
                    "used": d.used,
                    "free": d.free,
                    "percent": d.percent,
                }
            except Exception:
                return None

        root = _usage("/") or {"mount": "/", "total": 0, "used": 0, "free": 0, "percent": 0}
        workspace = _usage("/workspace")

        result: Dict[str, Any] = {"root": root}
        if workspace:
            result["workspace"] = workspace
        return result

    @staticmethod
    def get_cpu_info() -> Dict[str, Any]:
        """Get information about CPU"""
        cpu_freq = psutil.cpu_freq()
        return {
            "name": "CPU",
            "frequency": cpu_freq.current if cpu_freq else 0,
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
            "percent": psutil.cpu_percent(interval=1),
        }

    @staticmethod
    def get_gpu_info() -> Dict[str, Any]:
        # Try 1: nvidia-ml-py
        logger.debug("Attempting GPU info retrieval via nvidia-ml-py")
        try:
            import pynvml as nvml
            nvml.nvmlInit()
            count = nvml.nvmlDeviceGetCount()
            gpus = []
            for i in range(count):
                handle = nvml.nvmlDeviceGetHandleByIndex(i)
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                util = nvml.nvmlDeviceGetUtilizationRates(handle)
                gpus.append({
                    "id": i,
                    "name": nvml.nvmlDeviceGetName(handle),
                    "memory_total": mem.total,
                    "memory_used": mem.used,
                    "memory_free": mem.free,
                    "load": util.gpu,
                    "memory_percent": round(mem.used / mem.total * 100, 1) if mem.total else 0,
                })
            logger.debug("GPU info retrieved via nvidia-ml-py | count=%d", count)
            return {"count": count, "gpus": gpus}
        except Exception:
            pass

        # Try 2: nvidia-smi subprocess
        logger.debug("nvidia-ml-py unavailable, falling back to nvidia-smi")
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                gpus = []
                mib = 1024 * 1024
                for i, line in enumerate(result.stdout.strip().splitlines()):
                    name, mem_total, mem_used, mem_free, load = line.split(", ")
                    mt = int(mem_total) * mib
                    mu = int(mem_used) * mib
                    gpus.append({
                        "id": i,
                        "name": name,
                        "memory_total": mt,
                        "memory_used": mu,
                        "memory_free": int(mem_free) * mib,
                        "load": int(load),
                        "memory_percent": round(mu / mt * 100, 1) if mt else 0,
                    })
                logger.debug("GPU info via nvidia-smi | count=%d", len(gpus))
                return {"count": len(gpus), "gpus": gpus}
        except Exception:
            pass

        logger.warning("GPU info unavailable")
        return {"count": 0, "gpus": []}

    @staticmethod
    def get_environment_info() -> Dict[str, Any]:
        """Get Python, CUDA, PyTorch versions and hostname"""
        import socket

        # Python version
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # CUDA version
        cuda_version = "N/A"
        cuda_env = os.environ.get("CUDA_VERSION", "").strip()
        if cuda_env:
            cuda_version = cuda_env
        else:
            try:
                r = subprocess.run(["nvcc", "--version"], capture_output=True, text=True, timeout=5)
                for line in r.stdout.splitlines():
                    if "release" in line.lower():
                        parts = line.split("release")
                        if len(parts) > 1:
                            cuda_version = parts[1].split(",")[0].strip()
                            break
            except Exception:
                pass
            if cuda_version == "N/A":
                try:
                    r = subprocess.run(
                        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                        capture_output=True, text=True, timeout=5,
                    )
                    v = r.stdout.strip()
                    if v:
                        cuda_version = f"Driver {v}"
                except Exception:
                    pass

        # PyTorch version
        torch_version = "N/A"
        try:
            import torch
            torch_version = torch.__version__
        except ImportError:
            pass

        # Hostname
        hostname = socket.gethostname()

        return {
            "python_version": python_version,
            "cuda_version": cuda_version,
            "torch_version": torch_version,
            "hostname": hostname,
        }

    @staticmethod
    def get_all_system_info() -> Dict[str, Any]:
        """Get all system information"""
        logger.debug("Collecting full system info")
        return {
            "memory": DashboardService.get_memory_info(),
            "disk": DashboardService.get_disk_info(),
            "cpu": DashboardService.get_cpu_info(),
            "gpu": DashboardService.get_gpu_info(),
            "environment": DashboardService.get_environment_info(),
            "shutdown": ShutdownScheduler.status(),
        }