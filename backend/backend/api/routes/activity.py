"""
Unified Nesting Activity endpoint — aggregates all CPU nesting state
for the dashboard: refine batches, cutplan generation, Surface queue.
"""
import logging
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...database import get_db
from ...services.job_registry import refinement_jobs, cutplan_jobs
from ..deps import get_current_user
from ...models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nesting", tags=["activity"])


def _get_surface_queue_snapshot() -> Dict:
    """Safely get Surface queue state (may not be initialized)."""
    try:
        from ...services.surface_queue import SurfaceNestingQueue
        sq = SurfaceNestingQueue.get()
        return {
            "paused": not sq._paused.is_set(),
            "current_job_id": sq._current.id if sq._current else None,
            "queued_count": sq._queue.qsize(),
        }
    except Exception:
        return {"paused": False, "current_job_id": None, "queued_count": 0}


def _get_surface_jobs_by_cutplan() -> Dict[str, list]:
    """Get Surface jobs grouped by cutplan_id."""
    try:
        from ...services.surface_queue import SurfaceNestingQueue
        sq = SurfaceNestingQueue.get()
        groups = sq.jobs_by_cutplan()
        result = {}
        for cp_id, jobs in groups.items():
            result[cp_id] = []
            for j in jobs:
                now = time.time()
                elapsed = (now - j.started_at) if j.started_at else 0
                rc = j.params.get("remote_config", {})
                tl = rc.get("time_limit_s", 0)
                exp = rc.get("exploration_time", 0)
                comp = rc.get("compression_time", 0)
                estimated = j.time_limit_override or max(tl, exp + comp) or 2400
                result[cp_id].append({
                    "surface_job_id": j.id,
                    "marker_label": j.marker_label,
                    "ratio_str": j.params.get("label", ""),
                    "status": j.status,
                    "elapsed_s": elapsed,
                    "estimated_time_s": estimated,
                    "utilization": j.result.get("utilization") if j.result else None,
                })
        return result
    except Exception:
        return {}


def _build_refine_batches() -> List[Dict]:
    """Build refine batch list from refinement_jobs + Surface queue data."""
    surface_by_cp = _get_surface_jobs_by_cutplan()
    batches = []

    for cutplan_id, job in list(refinement_jobs.items()):
        status = job.get("status", "unknown")
        # Skip old completed/failed jobs older than 2 hours
        if status in ("completed", "failed", "cancelled"):
            started = job.get("started_at", 0)
            if started and time.time() - started > 7200:
                continue

        markers_total = job.get("markers_total", 0)
        markers_done = job.get("markers_done", 0)
        completed_markers = job.get("completed_markers", [])
        backend = job.get("backend", "local")

        # Build per-marker list
        marker_list: List[Dict] = []

        # Add completed markers
        for cm in completed_markers:
            marker_list.append({
                "marker_label": cm.get("marker_label", ""),
                "ratio_str": cm.get("ratio_str", ""),
                "status": "completed",
                "elapsed_s": cm.get("computation_time_s", 0),
                "estimated_time_s": 0,
                "utilization": cm.get("utilization"),
                "surface_job_id": None,
            })

        # For Surface backend, merge running/queued Surface jobs
        if backend == "surface" and cutplan_id in surface_by_cp:
            for sj in surface_by_cp[cutplan_id]:
                marker_list.append({
                    "marker_label": sj["marker_label"],
                    "ratio_str": sj["ratio_str"],
                    "status": sj["status"],  # running or queued
                    "elapsed_s": sj["elapsed_s"],
                    "estimated_time_s": sj["estimated_time_s"],
                    "utilization": sj["utilization"],
                    "surface_job_id": sj["surface_job_id"],
                })
        elif backend == "local" and status == "running":
            # For local backend, infer the currently running marker
            if markers_done < markers_total:
                running_idx = markers_done
                marker_list.append({
                    "marker_label": f"M{running_idx + 1}",
                    "ratio_str": "",
                    "status": "running",
                    "elapsed_s": 0,
                    "estimated_time_s": 0,
                    "utilization": None,
                    "surface_job_id": None,
                })
            # Remaining queued markers
            for i in range(markers_done + 1, markers_total):
                marker_list.append({
                    "marker_label": f"M{i + 1}",
                    "ratio_str": "",
                    "status": "queued",
                    "elapsed_s": 0,
                    "estimated_time_s": 0,
                    "utilization": None,
                    "surface_job_id": None,
                })

        batches.append({
            "cutplan_id": cutplan_id,
            "cutplan_name": job.get("cutplan_name", ""),
            "order_id": job.get("order_id", ""),
            "order_number": job.get("order_number", ""),
            "backend": backend,
            "status": status,
            "progress": job.get("progress", 0),
            "markers_total": markers_total,
            "markers_done": markers_done,
            "started_at": job.get("started_at"),
            "markers": marker_list,
        })

    # Sort: running first, then by started_at desc
    status_order = {"running": 0, "completed": 2, "failed": 3, "cancelled": 3}
    batches.sort(key=lambda b: (status_order.get(b["status"], 1), -(b.get("started_at") or 0)))

    return batches


def _build_quick_nests() -> List[Dict]:
    """Build quick nest (cutplan generation) status list."""
    nests = []
    for order_id, job in list(cutplan_jobs.items()):
        status = job.get("status", "unknown")
        # Only show running or recently completed
        if status in ("completed", "failed", "cancelled"):
            started = job.get("started_at", 0)
            if started and time.time() - started > 300:  # 5 min expiry
                continue

        nests.append({
            "order_id": order_id,
            "order_number": job.get("order_number", order_id[:8]),
            "status": status,
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "phase": job.get("phase", "ilp"),
        })

    return nests


@router.get("/activity")
async def get_nesting_activity(
    current_user: User = Depends(get_current_user),
):
    """Unified nesting activity dashboard — all CPU nesting state in one call."""
    return {
        "surface_queue": _get_surface_queue_snapshot(),
        "refine_batches": _build_refine_batches(),
        "quick_nests": _build_quick_nests(),
    }
