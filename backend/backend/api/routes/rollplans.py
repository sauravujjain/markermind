"""
Roll Plan API endpoints.

Provides Monte Carlo simulation and GA optimization for fabric roll usage
against an approved cutplan.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session, joinedload

import statistics
import time
import traceback

from ...database import get_db, SessionLocal
from ...models import User, Cutplan, Order
from ...models.cutplan import CutplanStatus
from ...models.rollplan import (
    FabricRoll,
    RollPlan,
    RollPlanMode,
    RollPlanStatus,
    RollInputType,
)
from ...schemas.rollplan import (
    RollPlanCreateRequest,
    RollPlanResponse,
    RollPlanStatusResponse,
    RollPlanListItem,
    RollUploadResponse,
    FabricRollResponse,
    CutDocketResponse,
)
from ...services.rollplan_service import RollPlanService
from ..deps import get_current_user

router = APIRouter(prefix="/rollplans", tags=["rollplans"])
rollplan_service = RollPlanService()

# In-memory job tracking (matches cutplan pattern)
_rollplan_jobs: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# Background execution
# ---------------------------------------------------------------------------


def execute_rollplan_job(
    rollplan_id: str,
    ga_pop_size: int = 30,
    ga_generations: int = 50,
):
    """Execute roll plan simulation in the background."""
    db = SessionLocal()
    try:
        roll_plan = db.query(RollPlan).filter(RollPlan.id == rollplan_id).first()
        if not roll_plan:
            _rollplan_jobs[rollplan_id] = {
                "status": "failed", "progress": 0, "message": "Roll plan not found",
            }
            return

        roll_plan.status = RollPlanStatus.running
        roll_plan.progress = 0
        roll_plan.progress_message = "Starting simulation..."
        db.commit()

        _rollplan_jobs[rollplan_id] = {
            "status": "running",
            "progress": 0,
            "message": "Starting simulation...",
            "started_at": time.time(),
        }

        def progress_callback(pct: int, message: str):
            _rollplan_jobs[rollplan_id]["progress"] = pct
            _rollplan_jobs[rollplan_id]["message"] = message
            roll_plan.progress = pct
            roll_plan.progress_message = message
            db.commit()

        def cancel_check() -> bool:
            return _rollplan_jobs.get(rollplan_id, {}).get("status") == "cancelled"

        rollplan_service.run_simulation(
            db=db,
            roll_plan=roll_plan,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            ga_pop_size=ga_pop_size,
            ga_generations=ga_generations,
        )

        if cancel_check():
            roll_plan.status = RollPlanStatus.cancelled
            roll_plan.progress_message = "Cancelled by user"
            _rollplan_jobs[rollplan_id].update({
                "status": "cancelled", "message": "Cancelled by user",
            })
        else:
            elapsed = time.time() - _rollplan_jobs[rollplan_id]["started_at"]
            msg = f"Complete in {elapsed:.0f}s"
            _rollplan_jobs[rollplan_id].update({
                "status": "completed", "progress": 100, "message": msg,
            })

        db.commit()

    except Exception as e:
        traceback.print_exc()
        elapsed = time.time() - _rollplan_jobs.get(rollplan_id, {}).get("started_at", time.time())
        err_msg = f"Failed after {elapsed:.0f}s: {str(e)}"

        _rollplan_jobs[rollplan_id] = {
            "status": "failed",
            "progress": _rollplan_jobs.get(rollplan_id, {}).get("progress", 0),
            "message": err_msg,
        }

        try:
            roll_plan = db.query(RollPlan).filter(RollPlan.id == rollplan_id).first()
            if roll_plan:
                roll_plan.status = RollPlanStatus.failed
                roll_plan.error_message = err_msg
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=dict)
async def create_rollplan(
    request: RollPlanCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a roll plan (config only). Upload rolls and start simulation separately."""
    # Verify cutplan exists and belongs to user
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == request.cutplan_id,
        Order.customer_id == current_user.customer_id,
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    # Map mode string
    mode_map = {
        "monte_carlo": RollPlanMode.monte_carlo,
        "ga": RollPlanMode.ga,
        "both": RollPlanMode.both,
    }
    mode = mode_map.get(request.mode, RollPlanMode.both)

    roll_plan = RollPlan(
        cutplan_id=request.cutplan_id,
        name=request.name or f"Roll Plan - {cutplan.name or 'untitled'}",
        color_code=request.color_code,
        mode=mode,
        num_simulations=request.num_simulations,
        min_reuse_length_yards=request.min_reuse_length_yards,
        pseudo_roll_avg_yards=request.pseudo_roll_avg_yards,
        pseudo_roll_delta_yards=request.pseudo_roll_delta_yards,
        status=RollPlanStatus.pending,
        input_type=RollInputType.pseudo,
    )
    db.add(roll_plan)
    db.commit()
    db.refresh(roll_plan)

    return {"id": roll_plan.id, "status": "pending", "message": "Roll plan created"}


@router.post("/{rollplan_id}/upload-rolls", response_model=RollUploadResponse)
async def upload_rolls(
    rollplan_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload roll inventory Excel for a roll plan."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    if roll_plan.status not in (RollPlanStatus.pending, RollPlanStatus.completed, RollPlanStatus.failed):
        raise HTTPException(status_code=400, detail="Cannot upload rolls while simulation is running")

    file_bytes = await file.read()
    try:
        records = rollplan_service.upload_rolls(db, rollplan_id, file_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    lengths = [r.length_yards for r in records]
    return RollUploadResponse(
        roll_plan_id=rollplan_id,
        rolls_count=len(records),
        total_length_yards=round(sum(lengths), 2),
        avg_length_yards=round(statistics.mean(lengths), 2) if lengths else 0,
        median_length_yards=round(statistics.median(lengths), 2) if lengths else 0,
        min_length_yards=round(min(lengths), 2) if lengths else 0,
        max_length_yards=round(max(lengths), 2) if lengths else 0,
        rolls=[FabricRollResponse.model_validate(r) for r in records],
    )


@router.post("/{rollplan_id}/simulate", response_model=dict)
async def start_simulation(
    rollplan_id: str,
    background_tasks: BackgroundTasks,
    ga_pop_size: int = 30,
    ga_generations: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start MC/GA simulation in the background."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    if roll_plan.status == RollPlanStatus.running:
        raise HTTPException(status_code=409, detail="Simulation already running")

    # Reset for re-run
    roll_plan.status = RollPlanStatus.pending
    roll_plan.progress = 0
    roll_plan.progress_message = "Queued..."
    roll_plan.error_message = None
    db.commit()

    background_tasks.add_task(
        execute_rollplan_job,
        rollplan_id=rollplan_id,
        ga_pop_size=ga_pop_size,
        ga_generations=ga_generations,
    )

    return {"id": rollplan_id, "status": "queued", "message": "Simulation started"}


@router.get("/{rollplan_id}/status", response_model=RollPlanStatusResponse)
async def get_status(
    rollplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll simulation progress."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    # Prefer in-memory job state (more current), fall back to DB
    job = _rollplan_jobs.get(rollplan_id)
    if job:
        return RollPlanStatusResponse(
            id=rollplan_id,
            status=job.get("status", "unknown"),
            progress=job.get("progress", 0),
            message=job.get("message", ""),
        )

    return RollPlanStatusResponse(
        id=rollplan_id,
        status=roll_plan.status.value if roll_plan.status else "pending",
        progress=roll_plan.progress or 0,
        message=roll_plan.progress_message or "",
    )


@router.get("/{rollplan_id}", response_model=RollPlanResponse)
async def get_rollplan(
    rollplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get full roll plan results."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)
    data = rollplan_service.build_response_data(db, roll_plan)
    return RollPlanResponse(**data)


@router.get("", response_model=List[RollPlanListItem])
async def list_rollplans(
    cutplan_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List roll plans, optionally filtered by cutplan."""
    query = (
        db.query(RollPlan)
        .join(Cutplan, RollPlan.cutplan_id == Cutplan.id)
        .join(Order, Cutplan.order_id == Order.id)
        .filter(Order.customer_id == current_user.customer_id)
    )
    if cutplan_id:
        query = query.filter(RollPlan.cutplan_id == cutplan_id)

    plans = query.order_by(RollPlan.created_at.desc()).all()
    return [RollPlanListItem.model_validate(p) for p in plans]


@router.get("/{rollplan_id}/dockets", response_model=List[CutDocketResponse])
async def get_dockets(
    rollplan_id: str,
    source: str = "mc",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get cutting dockets from a completed roll plan.

    Query param `source`: "mc" for Monte Carlo best run, "ga" for GA optimizer.
    """
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    if roll_plan.status != RollPlanStatus.completed:
        raise HTTPException(status_code=400, detail="Simulation not completed yet")

    if source == "ga":
        dockets = roll_plan.ga_dockets
    else:
        dockets = roll_plan.mc_best_run_dockets

    if not dockets:
        return []

    return [CutDocketResponse(**d) for d in dockets]


@router.get("/{rollplan_id}/dockets/{cut_number}", response_model=CutDocketResponse)
async def get_single_docket(
    rollplan_id: str,
    cut_number: int,
    source: str = "mc",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single cutting docket by cut number."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    if roll_plan.status != RollPlanStatus.completed:
        raise HTTPException(status_code=400, detail="Simulation not completed yet")

    if source == "ga":
        dockets = roll_plan.ga_dockets
    else:
        dockets = roll_plan.mc_best_run_dockets

    if not dockets:
        raise HTTPException(status_code=404, detail="No dockets available")

    for d in dockets:
        if d.get("cut_number") == cut_number:
            return CutDocketResponse(**d)

    raise HTTPException(status_code=404, detail=f"Cut number {cut_number} not found")


@router.get("/{rollplan_id}/rolls", response_model=List[FabricRollResponse])
async def get_rolls(
    rollplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all rolls (real + pseudo) for a roll plan."""
    _get_rollplan_or_404(db, rollplan_id, current_user)
    rolls = (
        db.query(FabricRoll)
        .filter(FabricRoll.roll_plan_id == rollplan_id)
        .order_by(FabricRoll.roll_number)
        .all()
    )
    return [FabricRollResponse.model_validate(r) for r in rolls]


@router.post("/{rollplan_id}/cancel", response_model=dict)
async def cancel_simulation(
    rollplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a running simulation."""
    _get_rollplan_or_404(db, rollplan_id, current_user)

    job = _rollplan_jobs.get(rollplan_id)
    if not job or job.get("status") != "running":
        raise HTTPException(status_code=400, detail="No running simulation to cancel")

    _rollplan_jobs[rollplan_id]["status"] = "cancelled"
    _rollplan_jobs[rollplan_id]["message"] = "Cancelling..."
    return {"message": "Cancellation requested", "status": "cancelled"}


@router.delete("/{rollplan_id}", response_model=dict)
async def delete_rollplan(
    rollplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a roll plan and its rolls."""
    roll_plan = _get_rollplan_or_404(db, rollplan_id, current_user)

    if roll_plan.status == RollPlanStatus.running:
        raise HTTPException(status_code=400, detail="Cannot delete while simulation is running")

    db.delete(roll_plan)
    db.commit()
    return {"message": "Roll plan deleted"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_rollplan_or_404(db: Session, rollplan_id: str, current_user: User) -> RollPlan:
    """Load roll plan with ownership check via cutplan→order→customer."""
    roll_plan = (
        db.query(RollPlan)
        .join(Cutplan, RollPlan.cutplan_id == Cutplan.id)
        .join(Order, Cutplan.order_id == Order.id)
        .filter(
            RollPlan.id == rollplan_id,
            Order.customer_id == current_user.customer_id,
        )
        .first()
    )
    if not roll_plan:
        raise HTTPException(status_code=404, detail="Roll plan not found")
    return roll_plan
