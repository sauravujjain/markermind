from typing import List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
import time
import traceback

from ...database import get_db, SessionLocal
from ...schemas.cutplan import (
    CutplanOptimizeRequest, CutplanResponse,
    CutplanMarkerResponse, CostBreakdownResponse
)
from ...models import User, Cutplan, CutplanMarker, Order, MarkerBank
from ...services.cutplan_service import CutplanService
from ..deps import get_current_user

router = APIRouter(prefix="/cutplans", tags=["cutplans"])
cutplan_service = CutplanService()

# In-memory status for cutplan generation jobs
# Key: order_id, Value: {status, progress, message, started_at, strategies_total, strategies_done}
_cutplan_jobs: Dict[str, Dict] = {}


def execute_cutplan_job(
    order_id: str,
    pattern_id: str,
    fabric_id: str,
    customer_id: str,
    strategies: List[str],
    penalty: float,
    color_code: Optional[str] = None,
    fabric_cost_per_yard: Optional[float] = None,
):
    """Execute cutplan optimization in the background."""
    db = SessionLocal()
    try:
        color_label = f" for color {color_code}" if color_code else ""
        _cutplan_jobs[order_id] = {
            "status": "running",
            "progress": 0,
            "message": f"Starting cutplan optimization{color_label}...",
            "started_at": time.time(),
            "strategies_total": len(strategies),
            "strategies_done": 0,
        }

        def progress_callback(pct: int, message: str):
            _cutplan_jobs[order_id]["progress"] = pct
            _cutplan_jobs[order_id]["message"] = message
            # Parse "Strategy X/Y done:" to update strategies_done for incremental display
            if "done:" in message:
                try:
                    part = message.split("done:")[0]  # "Strategy 1/3 "
                    done_str = part.strip().split()[-1].split("/")[0]  # "1"
                    _cutplan_jobs[order_id]["strategies_done"] = int(done_str)
                except (ValueError, IndexError):
                    pass

        def cancel_check() -> bool:
            return _cutplan_jobs.get(order_id, {}).get("status") == "cancelled"

        cutplans = cutplan_service.run_multi_strategy_optimization(
            db=db,
            order_id=order_id,
            pattern_id=pattern_id,
            fabric_id=fabric_id,
            customer_id=customer_id,
            strategies=strategies,
            penalty=penalty,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            color_code=color_code,
            fabric_cost_per_yard=fabric_cost_per_yard,
        )

        elapsed = time.time() - _cutplan_jobs[order_id]["started_at"]
        _cutplan_jobs[order_id].update({
            "status": "completed",
            "progress": 100,
            "message": f"Done — {len(cutplans)} options in {elapsed:.0f}s",
            "strategies_done": len(cutplans),
        })

    except Exception as e:
        traceback.print_exc()
        elapsed = time.time() - _cutplan_jobs.get(order_id, {}).get("started_at", time.time())
        _cutplan_jobs[order_id] = {
            "status": "failed",
            "progress": _cutplan_jobs.get(order_id, {}).get("progress", 0),
            "message": f"Failed after {elapsed:.0f}s: {str(e)}",
            "strategies_total": len(strategies),
            "strategies_done": 0,
        }
    finally:
        db.close()


@router.post("/optimize", response_model=List[CutplanResponse])
async def optimize_cutplan(
    request: CutplanOptimizeRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start cutplan optimization in the background. Returns existing cutplans immediately."""
    # Verify order exists
    order = db.query(Order).filter(
        Order.id == request.order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.pattern_id:
        raise HTTPException(status_code=400, detail="Order has no pattern linked")

    # Check if already running
    existing_job = _cutplan_jobs.get(request.order_id)
    if existing_job and existing_job.get("status") == "running":
        raise HTTPException(status_code=409, detail="Cutplan optimization already running for this order")

    # Delete any existing cutplans for this order (re-generation)
    existing_cutplans = db.query(Cutplan).filter(
        Cutplan.order_id == request.order_id,
        Cutplan.status.notin_(["approved", "in_production", "completed"])
    ).all()
    for cp in existing_cutplans:
        db.delete(cp)
    db.commit()

    # Get fabric_id from order line (filtered by color if specified)
    fabric_id = None
    target_lines = order.order_lines
    if request.color_code:
        target_lines = [l for l in order.order_lines if l.color_code == request.color_code]
        if not target_lines:
            raise HTTPException(status_code=400, detail=f"No order lines found for color '{request.color_code}'")

    if target_lines:
        first_line = target_lines[0]
        fabric_id = first_line.fabric_id
        if not fabric_id and first_line.fabric_code:
            from ...models import Fabric
            fabric = db.query(Fabric).filter(
                Fabric.code == first_line.fabric_code,
                Fabric.customer_id == current_user.customer_id
            ).first()
            if fabric:
                fabric_id = fabric.id

    if not fabric_id:
        raise HTTPException(status_code=400, detail="Order has no fabric configured")

    # Map strategy names
    strategy_map = {
        "max_efficiency": "max_efficiency",
        "efficiency": "max_efficiency",
        "balanced": "balanced",
        "min_markers": "min_markers",
        "min_plies": "min_plies",
        "min_bundle_cuts": "min_bundle_cuts",
    }

    strategies = []
    for opt in (request.generate_options or ["max_efficiency", "balanced", "min_markers"]):
        mapped = strategy_map.get(opt.lower(), opt.lower())
        if mapped not in strategies:
            strategies.append(mapped)

    # Launch in background
    background_tasks.add_task(
        execute_cutplan_job,
        order_id=request.order_id,
        pattern_id=order.pattern_id,
        fabric_id=fabric_id,
        customer_id=current_user.customer_id,
        strategies=strategies,
        penalty=request.penalty or 5.0,
        color_code=request.color_code,
        fabric_cost_per_yard=request.fabric_cost_per_yard,
    )

    # Return empty list — frontend will poll for results
    return []


@router.get("/optimize-status/{order_id}")
async def get_optimize_status(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get cutplan optimization status for an order."""
    # Verify order
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    job = _cutplan_jobs.get(order_id)
    if not job:
        return {
            "status": "idle",
            "progress": 0,
            "message": "No optimization running",
            "strategies_total": 0,
            "strategies_done": 0,
        }

    return {
        "status": job.get("status", "unknown"),
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "strategies_total": job.get("strategies_total", 0),
        "strategies_done": job.get("strategies_done", 0),
    }


@router.post("/optimize-cancel/{order_id}")
async def cancel_optimize(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a running cutplan optimization."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    job = _cutplan_jobs.get(order_id)
    if not job or job.get("status") != "running":
        raise HTTPException(status_code=400, detail="No running optimization to cancel")

    _cutplan_jobs[order_id]["status"] = "cancelled"
    _cutplan_jobs[order_id]["message"] = "Cancelling..."
    return {"message": "Cancellation requested", "status": "cancelled"}


@router.get("/{cutplan_id}", response_model=CutplanResponse)
async def get_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get cutplan by ID."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")
    return cutplan


@router.get("", response_model=List[CutplanResponse])
async def list_cutplans(
    order_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List cutplans."""
    query = db.query(Cutplan).join(Order).filter(
        Order.customer_id == current_user.customer_id
    )
    if order_id:
        query = query.filter(Cutplan.order_id == order_id)
    if status:
        query = query.filter(Cutplan.status == status)

    cutplans = query.order_by(Cutplan.created_at.desc()).offset(skip).limit(limit).all()
    return cutplans


@router.post("/{cutplan_id}/approve", response_model=CutplanResponse)
async def approve_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Approve a cutplan for production."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    if cutplan.status != "ready":
        raise HTTPException(status_code=400, detail="Cutplan must be in ready status")

    cutplan = cutplan_service.approve_cutplan(db, cutplan_id)
    return cutplan


@router.delete("/{cutplan_id}")
async def delete_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a cutplan."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    if cutplan.status in ["approved", "in_production", "completed"]:
        raise HTTPException(status_code=400, detail="Cannot delete approved/in-production cutplan")

    db.delete(cutplan)
    db.commit()
    return {"message": "Cutplan deleted"}


@router.get("/{cutplan_id}/cost-analysis", response_model=CostBreakdownResponse)
async def get_cost_analysis(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get detailed cost breakdown for a cutplan."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    return CostBreakdownResponse(
        total_cost=cutplan.total_cost or 0,
        fabric_cost=cutplan.fabric_cost or 0,
        spreading_cost=cutplan.spreading_cost or 0,
        cutting_cost=cutplan.cutting_cost or 0,
        prep_cost=cutplan.prep_cost or 0,
        fabric_yards=cutplan.total_yards or 0,
        total_plies=cutplan.total_plies or 0,
        unique_markers=cutplan.unique_markers or 0,
    )
