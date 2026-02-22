import logging
from typing import List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
import io
import os
import time
import traceback
import zipfile

logger = logging.getLogger(__name__)

from ...database import get_db, SessionLocal
from ...config import settings, resolve_path
from ...schemas.cutplan import (
    CutplanOptimizeRequest, CutplanResponse,
    CutplanMarkerResponse, CostBreakdownResponse,
    RefinementRequest, MarkerLayoutResponse, RefinementStatusResponse,
)
from ...models import User, Cutplan, CutplanMarker, Order, MarkerBank, MarkerLayout
from ...models.cutplan import CutplanStatus
from ...services.cutplan_service import CutplanService
from ..deps import get_current_user

router = APIRouter(prefix="/cutplans", tags=["cutplans"])
cutplan_service = CutplanService()

# In-memory status for cutplan generation jobs
# Key: order_id, Value: {status, progress, message, started_at, strategies_total, strategies_done}
_cutplan_jobs: Dict[str, Dict] = {}

# In-memory status for refinement jobs
# Key: cutplan_id, Value: {status, progress, message, markers_total, markers_done, started_at}
_refinement_jobs: Dict[str, Dict] = {}


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

    if cutplan.status not in ("ready", "approved"):
        raise HTTPException(status_code=400, detail=f"Cutplan must be in ready or approved status (current: {cutplan.status})")

    if cutplan.status == "approved":
        return cutplan  # Already approved, idempotent

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


# --------------------------------------------------------------------------
# Final Nesting (Spyrrow CPU Refinement)
# --------------------------------------------------------------------------

def execute_refinement_job(
    cutplan_id: str,
    customer_id: str,
    piece_buffer_mm: float,
    edge_buffer_mm: float,
    time_limit_s: float,
    rotation_mode: str,
):
    """Execute CPU refinement in the background."""
    db = SessionLocal()
    try:
        # Load cutplan with markers
        cutplan = db.query(Cutplan).options(
            joinedload(Cutplan.markers),
            joinedload(Cutplan.order),
        ).filter(Cutplan.id == cutplan_id).first()

        if not cutplan:
            _refinement_jobs[cutplan_id] = {
                "status": "failed", "message": "Cutplan not found",
                "markers_total": 0, "markers_done": 0, "progress": 0,
            }
            return

        order = cutplan.order
        markers = cutplan.markers

        _refinement_jobs[cutplan_id] = {
            "status": "running",
            "progress": 0,
            "message": f"Loading pieces for {len(markers)} markers...",
            "started_at": time.time(),
            "markers_total": len(markers),
            "markers_done": 0,
        }

        # Delete all existing layouts for this cutplan before re-running
        for m in markers:
            existing = db.query(MarkerLayout).filter(
                MarkerLayout.cutplan_marker_id == m.id
            ).first()
            if existing:
                db.delete(existing)
        db.flush()

        # Update cutplan status
        cutplan.status = CutplanStatus.refining
        db.commit()

        # Find pattern + fabric info
        from ...models import Pattern, Fabric, PatternFabricMapping
        pattern = db.query(Pattern).filter(Pattern.id == order.pattern_id).first()
        if not pattern or not pattern.dxf_file_path:
            raise ValueError("Pattern DXF file not found")

        dxf_path = resolve_path(pattern.dxf_file_path)
        rul_path = resolve_path(pattern.rul_file_path) if pattern.rul_file_path else None

        if not rul_path:
            raise ValueError("Pattern RUL file required for graded nesting")

        # Determine material and fabric width from order lines
        order_lines = order.order_lines
        if not order_lines:
            raise ValueError("Order has no order lines")

        first_line = order_lines[0]
        fabric_code = first_line.fabric_code
        fabric = db.query(Fabric).filter(
            Fabric.code == fabric_code,
            Fabric.customer_id == customer_id,
        ).first()

        fabric_width_mm = (fabric.width_inches * 25.4) if fabric else 1524.0  # Default 60"

        # Determine sizes from cutplan markers
        # Get sizes from order
        sizes_set = set()
        seen_colors = set()
        for line in order_lines:
            if line.color_code in seen_colors:
                continue
            seen_colors.add(line.color_code)
            for sq in line.size_quantities:
                if sq.quantity > 0:
                    sizes_set.add(sq.size_code)
        sizes = sorted(sizes_set)

        # Material = fabric_code (matches pattern material name)
        material = fabric_code

        # Prepare marker list for refinement
        marker_dicts = [{"ratio_str": m.ratio_str, "id": m.id} for m in markers]

        # DXF output directory
        dxf_dir = os.path.join(resolve_path(settings.upload_dir), "markers", cutplan_id)
        os.makedirs(dxf_dir, exist_ok=True)

        from ...services.spyrrow_nesting_runner import refine_cutplan_markers

        def progress_cb(marker_idx, total, result):
            pct = int((marker_idx + 1) / total * 100)
            _refinement_jobs[cutplan_id].update({
                "progress": pct,
                "markers_done": marker_idx + 1,
                "message": f"Completed {result['marker_label']} ({result['utilization']*100:.1f}%) — {marker_idx + 1}/{total}",
            })

            # Save result to DB immediately
            marker_record = markers[marker_idx]
            dxf_filename = f"{result['marker_label']}.dxf"
            dxf_filepath = os.path.join(dxf_dir, dxf_filename)

            # Write DXF file
            with open(dxf_filepath, 'wb') as f:
                f.write(result['dxf_bytes'])

            # Delete existing layout if any
            existing_layout = db.query(MarkerLayout).filter(
                MarkerLayout.cutplan_marker_id == marker_record.id
            ).first()
            if existing_layout:
                db.delete(existing_layout)
                db.flush()

            # Create MarkerLayout record
            layout = MarkerLayout(
                cutplan_marker_id=marker_record.id,
                utilization=result['utilization'],
                strip_length_mm=result['strip_length_mm'],
                length_yards=result['length_yards'],
                computation_time_s=result['computation_time_s'],
                svg_preview=result['svg_preview'],
                dxf_file_path=dxf_filepath,
                piece_buffer_mm=piece_buffer_mm,
                edge_buffer_mm=edge_buffer_mm,
                time_limit_s=time_limit_s,
                rotation_mode=rotation_mode,
            )
            db.add(layout)
            db.commit()

        def cancel_cb():
            return _refinement_jobs.get(cutplan_id, {}).get("status") == "cancelled"

        results = refine_cutplan_markers(
            dxf_path=dxf_path,
            rul_path=rul_path,
            material=material,
            sizes=sizes,
            markers=marker_dicts,
            fabric_width_mm=fabric_width_mm,
            piece_buffer_mm=piece_buffer_mm,
            edge_buffer_mm=edge_buffer_mm,
            time_limit=time_limit_s,
            rotation_mode=rotation_mode,
            progress_callback=progress_cb,
            cancel_check=cancel_cb,
        )

        elapsed = time.time() - _refinement_jobs[cutplan_id]["started_at"]

        # Update cutplan status
        if cancel_cb():
            cutplan.status = CutplanStatus.approved  # Revert to approved
            _refinement_jobs[cutplan_id].update({
                "status": "cancelled",
                "message": f"Cancelled after {len(results)}/{len(markers)} markers ({elapsed:.0f}s)",
            })
        else:
            cutplan.status = CutplanStatus.refined
            _refinement_jobs[cutplan_id].update({
                "status": "completed",
                "progress": 100,
                "markers_done": len(results),
                "message": f"All {len(results)} markers refined in {elapsed:.0f}s",
            })
        db.commit()

    except BaseException as e:
        # BaseException catches pyo3_runtime.PanicException (from jagua-rs/Spyrrow
        # panics) which does NOT inherit from Exception, only from BaseException.
        traceback.print_exc()
        elapsed = time.time() - _refinement_jobs.get(cutplan_id, {}).get("started_at", time.time())
        _refinement_jobs[cutplan_id] = {
            "status": "failed",
            "progress": _refinement_jobs.get(cutplan_id, {}).get("progress", 0),
            "message": f"Failed after {elapsed:.0f}s: {str(e)}",
            "markers_total": _refinement_jobs.get(cutplan_id, {}).get("markers_total", 0),
            "markers_done": _refinement_jobs.get(cutplan_id, {}).get("markers_done", 0),
        }
        # Revert cutplan status
        try:
            cutplan = db.query(Cutplan).filter(Cutplan.id == cutplan_id).first()
            if cutplan:
                cutplan.status = CutplanStatus.approved
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/{cutplan_id}/refine")
async def start_refinement(
    cutplan_id: str,
    request: RefinementRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start CPU refinement for an approved cutplan. Runs in background."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id,
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    if cutplan.status not in ("approved", "refined"):
        raise HTTPException(status_code=400, detail=f"Cutplan must be approved to refine (current: {cutplan.status})")

    # Check if already running
    existing_job = _refinement_jobs.get(cutplan_id)
    if existing_job and existing_job.get("status") == "running":
        raise HTTPException(status_code=409, detail="Refinement already running for this cutplan")

    background_tasks.add_task(
        execute_refinement_job,
        cutplan_id=cutplan_id,
        customer_id=current_user.customer_id,
        piece_buffer_mm=request.piece_buffer_mm,
        edge_buffer_mm=request.edge_buffer_mm,
        time_limit_s=request.time_limit_s,
        rotation_mode=request.rotation_mode,
    )

    return {"message": "Refinement started", "cutplan_id": cutplan_id}


@router.get("/{cutplan_id}/refine-status")
async def get_refinement_status(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll for refinement progress. Returns markers done, total, and completed layouts."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id,
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    job = _refinement_jobs.get(cutplan_id)

    # Build layouts from DB (completed ones)
    layouts = []
    cutplan_markers = db.query(CutplanMarker).options(
        joinedload(CutplanMarker.layout)
    ).filter(
        CutplanMarker.cutplan_id == cutplan_id,
    ).all()

    for cm in cutplan_markers:
        if cm.layout:
            layouts.append(MarkerLayoutResponse(
                id=cm.layout.id,
                cutplan_marker_id=cm.id,
                ratio_str=cm.ratio_str,
                utilization=cm.layout.utilization or 0,
                strip_length_mm=cm.layout.strip_length_mm or 0,
                length_yards=cm.layout.length_yards or 0,
                computation_time_s=cm.layout.computation_time_s or 0,
                svg_preview=cm.layout.svg_preview or "",
                dxf_file_path=cm.layout.dxf_file_path,
                piece_buffer_mm=cm.layout.piece_buffer_mm,
                edge_buffer_mm=cm.layout.edge_buffer_mm,
                time_limit_s=cm.layout.time_limit_s,
                rotation_mode=cm.layout.rotation_mode,
            ))

    if not job:
        # No in-memory job — check for orphaned "refining" state
        # This happens when the backend reloads (--reload) mid-refinement,
        # wiping the in-memory _refinement_jobs dict while the DB stays "refining"
        if cutplan.status == CutplanStatus.refining:
            cutplan.status = CutplanStatus.approved
            db.commit()
            logger.warning(f"Reset orphaned 'refining' cutplan {cutplan_id} back to 'approved'")
            return RefinementStatusResponse(
                status="failed",
                progress=0,
                message="Refinement was interrupted (server reloaded). Please re-run.",
                markers_total=len(cutplan_markers),
                markers_done=len(layouts),
                layouts=layouts,
            )

        # Completed layouts from a previous run
        return RefinementStatusResponse(
            status="idle" if not layouts else "completed",
            progress=100 if layouts else 0,
            message="No refinement running" if not layouts else f"{len(layouts)} markers refined",
            markers_total=len(cutplan_markers),
            markers_done=len(layouts),
            layouts=layouts,
        )

    return RefinementStatusResponse(
        status=job.get("status", "unknown"),
        progress=job.get("progress", 0),
        message=job.get("message", ""),
        markers_total=job.get("markers_total", 0),
        markers_done=job.get("markers_done", 0),
        layouts=layouts,
    )


@router.post("/{cutplan_id}/refine-cancel")
async def cancel_refinement(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a running refinement job."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id,
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    job = _refinement_jobs.get(cutplan_id)
    if not job or job.get("status") != "running":
        raise HTTPException(status_code=400, detail="No running refinement to cancel")

    _refinement_jobs[cutplan_id]["status"] = "cancelled"
    _refinement_jobs[cutplan_id]["message"] = "Cancelling..."
    return {"message": "Cancellation requested", "status": "cancelled"}


@router.get("/{cutplan_id}/download-markers")
async def download_markers_dxf(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download all refined markers as a zip of DXF files."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id,
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    # Get all layouts
    cutplan_markers = db.query(CutplanMarker).options(
        joinedload(CutplanMarker.layout)
    ).filter(
        CutplanMarker.cutplan_id == cutplan_id,
    ).all()

    layouts_with_files = [
        (idx, cm) for idx, cm in enumerate(cutplan_markers)
        if cm.layout and cm.layout.dxf_file_path and os.path.exists(cm.layout.dxf_file_path)
    ]

    if not layouts_with_files:
        raise HTTPException(status_code=404, detail="No DXF files available for download")

    # Create zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, cm in layouts_with_files:
            dxf_path = cm.layout.dxf_file_path
            arcname = f"M{idx + 1}_{cm.ratio_str}.dxf"
            zf.write(dxf_path, arcname)

    zip_buffer.seek(0)

    order_number = cutplan.order.order_number if cutplan.order else "unknown"
    filename = f"{order_number}_{cutplan.name or 'cutplan'}_markers.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
