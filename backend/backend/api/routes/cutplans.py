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
    max_ply_height: Optional[int] = None,
    min_plies_by_bundle: Optional[str] = None,
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
            max_ply_height=max_ply_height,
            min_plies_by_bundle=min_plies_by_bundle,
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
        "min_plies": "min_end_cuts",  # Legacy alias
        "min_end_cuts": "min_end_cuts",
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
        max_ply_height=request.max_ply_height,
        min_plies_by_bundle=request.min_plies_by_bundle,
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
    cutplan = db.query(Cutplan).options(
        joinedload(Cutplan.markers)
    ).join(Order).filter(
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
    query = db.query(Cutplan).options(
        joinedload(Cutplan.markers)
    ).join(Order).filter(
        Order.customer_id == current_user.customer_id
    )
    if order_id:
        query = query.filter(Cutplan.order_id == order_id)
    if status:
        query = query.filter(Cutplan.status == status)

    cutplans = query.order_by(Cutplan.created_at.desc()).offset(skip).limit(limit).all()
    # De-duplicate from joinedload cartesian product
    seen = set()
    unique = []
    for c in cutplans:
        if c.id not in seen:
            seen.add(c.id)
            unique.append(c)
    return unique


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


def _recalculate_cutplan_costs(db: Session, cutplan: Cutplan, customer_id: str):
    """
    Recalculate cutplan costs after CPU refinement.

    After refinement, markers have updated lengths (from MarkerLayout).
    This recalculates total_yards, fabric_cost, prep_cost, and total_cost
    using the refined lengths while keeping cutting_cost unchanged
    (perimeter * cuts doesn't change with refinement).
    """
    from ...models import CostConfig as CostConfigModel, Pattern, Fabric as FabricModel, PatternFabricMapping
    import math

    cost_config = db.query(CostConfigModel).filter(
        CostConfigModel.customer_id == customer_id
    ).first()
    if not cost_config:
        return

    order = cutplan.order
    if not order:
        return

    # Get sizes from pattern
    pattern = db.query(Pattern).filter(Pattern.id == order.pattern_id).first() if order.pattern_id else None
    sizes = list(pattern.available_sizes) if pattern and pattern.available_sizes else []

    # Resolve perimeter_by_size for the material
    perimeter_by_size = {}
    if pattern and pattern.parse_metadata:
        perim_all = pattern.parse_metadata.get("perimeter_by_size", {})
        if perim_all and order.order_lines:
            first_line = order.order_lines[0]
            fab = db.query(FabricModel).filter(
                FabricModel.customer_id == customer_id,
                FabricModel.code == first_line.fabric_code,
            ).first()
            if fab:
                mapping = db.query(PatternFabricMapping).filter(
                    PatternFabricMapping.pattern_id == pattern.id,
                    PatternFabricMapping.fabric_id == fab.id,
                ).first()
                if mapping and mapping.material_name in perim_all:
                    perimeter_by_size = perim_all[mapping.material_name]
            if not perimeter_by_size and len(perim_all) == 1:
                perimeter_by_size = list(perim_all.values())[0]

    # Order sizes to match ratio_str positions
    order_sizes_set = set()
    seen_colors = set()
    for line in (order.order_lines or []):
        if line.color_code in seen_colors:
            continue
        seen_colors.add(line.color_code)
        for sq in line.size_quantities:
            if sq.quantity > 0:
                order_sizes_set.add(sq.size_code)
    if sizes:
        ordered_sizes = [s for s in sizes if s in order_sizes_set]
    else:
        ordered_sizes = sorted(order_sizes_set)

    # Cost parameters
    fabric_cost_per_yard = cost_config.fabric_cost_per_yard
    spreading_cost_per_yard = cost_config.spreading_cost_per_yard
    spreading_cost_per_ply = getattr(cost_config, 'spreading_cost_per_ply', 0.013)
    cutting_cost_per_cm = (
        (cost_config.cutting_labor_cost_per_hour * cost_config.cutting_workers_per_cut)
        / 3600.0
    ) / cost_config.cutting_speed_cm_per_s if cost_config.cutting_speed_cm_per_s > 0 else 0.0
    prep_cost_per_m = 0.0
    if getattr(cost_config, 'prep_perf_paper_enabled', True):
        prep_cost_per_m += getattr(cost_config, 'prep_perf_paper_cost_per_m', 0.1)
    if getattr(cost_config, 'prep_underlayer_enabled', True):
        prep_cost_per_m += getattr(cost_config, 'prep_underlayer_cost_per_m', 0.1)
    if getattr(cost_config, 'prep_top_layer_enabled', True):
        prep_cost_per_m += getattr(cost_config, 'prep_top_layer_cost_per_m', 0.05)
    max_ply_height = cost_config.max_ply_height or 100

    YARDS_TO_METERS = 0.9144
    AVG_PERIMETER_PER_BUNDLE_CM = 2540.0

    total_yards = 0.0
    total_plies = 0
    total_cuts = 0
    cutting_cost = 0.0
    prep_cost = 0.0

    for marker in cutplan.markers:
        marker_plies = marker.total_plies or 0
        marker_cuts = math.ceil(marker_plies / max_ply_height) if marker_plies > 0 else 0

        # Use refined length if available, else original
        if marker.layout and marker.layout.length_yards:
            length_yards = marker.layout.length_yards
        else:
            length_yards = marker.length_yards or 0

        total_yards += length_yards * marker_plies
        total_plies += marker_plies
        total_cuts += marker_cuts

        # Cutting cost: perimeter * cuts * rate
        # Prefer marker-level perimeter (from CPU refinement or GPU nesting)
        marker_perimeter_cm = None
        if marker.marker and marker.marker.extra_data:
            marker_perimeter_cm = marker.marker.extra_data.get("perimeter_cm")

        if not marker_perimeter_cm or marker_perimeter_cm <= 0:
            # Fallback to ratio × perimeter_by_size approach
            ratios = [int(x) for x in marker.ratio_str.split("-")]
            if perimeter_by_size and ordered_sizes and len(ordered_sizes) == len(ratios):
                marker_perimeter_cm = 0.0
                for i, count in enumerate(ratios):
                    if count > 0:
                        size = ordered_sizes[i]
                        marker_perimeter_cm += perimeter_by_size.get(size, AVG_PERIMETER_PER_BUNDLE_CM) * count
            else:
                bundle_count = sum(ratios)
                marker_perimeter_cm = bundle_count * AVG_PERIMETER_PER_BUNDLE_CM

        cutting_cost += marker_perimeter_cm * marker_cuts * cutting_cost_per_cm

        # Prep cost: length_m * cuts * rate
        length_m = length_yards * YARDS_TO_METERS
        prep_cost += length_m * marker_cuts * prep_cost_per_m

    fabric_cost = total_yards * fabric_cost_per_yard
    spreading_cost = (total_yards * spreading_cost_per_yard) + (total_plies * spreading_cost_per_ply)
    total_cost = fabric_cost + spreading_cost + cutting_cost + prep_cost

    # Update cutplan
    cutplan.total_yards = total_yards
    cutplan.total_plies = total_plies
    cutplan.total_cuts = total_cuts
    cutplan.fabric_cost = fabric_cost
    cutplan.spreading_cost = spreading_cost
    cutplan.cutting_cost = cutting_cost
    cutplan.prep_cost = prep_cost
    cutplan.total_cost = total_cost


def execute_refinement_job(
    cutplan_id: str,
    customer_id: str,
    piece_buffer_mm: float,
    edge_buffer_mm: float,
    time_limit_s: float,
    rotation_mode: str,
    quadtree_depth: int = 5,
    early_termination: bool = True,
    exploration_time_s: float = None,
    compression_time_s: float = None,
    seed_screening: bool = False,
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

        if not rul_path and pattern.file_type not in ("dxf_only", "vt_dxf"):
            raise ValueError("Pattern RUL file required for graded nesting")

        # Determine material and fabric width from order lines + nesting job
        order_lines = order.order_lines
        if not order_lines:
            raise ValueError("Order has no order lines")

        first_line = order_lines[0]
        fabric_code = first_line.fabric_code

        # Get fabric width from the most recent completed NestingJob for this order
        # (this is the user-specified width, not the generic Fabric table width)
        from ...models import NestingJob
        nesting_job = db.query(NestingJob).filter(
            NestingJob.order_id == order.id,
            NestingJob.status == "completed",
        ).order_by(NestingJob.created_at.desc()).first()

        if nesting_job and nesting_job.fabric_width_inches:
            fabric_width_mm = nesting_job.fabric_width_inches * 25.4
            print(f"[Refinement] Using NestingJob fabric width: {nesting_job.fabric_width_inches}\" ({fabric_width_mm:.1f}mm)")
        else:
            # Fallback: use Fabric table width (less reliable)
            fabric = db.query(Fabric).filter(
                Fabric.code == fabric_code,
                Fabric.customer_id == customer_id,
            ).first()
            fabric_width_mm = (fabric.width_inches * 25.4) if fabric else 1524.0
            print(f"[Refinement] WARNING: No NestingJob found, using Fabric table width: {fabric_width_mm / 25.4:.2f}\" ({fabric_width_mm:.1f}mm)")

        # Determine sizes — use pattern's canonical order (from RUL file)
        # to ensure ratio_str interpretation is consistent
        sizes_set = set()
        seen_colors = set()
        for line in order_lines:
            if line.color_code in seen_colors:
                continue
            seen_colors.add(line.color_code)
            for sq in line.size_quantities:
                if sq.quantity > 0:
                    sizes_set.add(sq.size_code)
        if pattern.available_sizes:
            # Preserve pattern order, filter to sizes actually in the order
            sizes = [s for s in pattern.available_sizes if s in sizes_set]
        else:
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
            marker_record = markers[marker_idx]
            label = marker_record.marker_label or f"M{marker_idx + 1}"
            _refinement_jobs[cutplan_id].update({
                "progress": pct,
                "markers_done": marker_idx + 1,
                "message": f"Completed {label} ({result['utilization']*100:.1f}%) — {marker_idx + 1}/{total}",
            })

            # Save result to DB immediately
            dxf_filename = f"{label}.dxf"
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
                quadtree_depth=quadtree_depth,
            )
            db.add(layout)

            # Update MarkerBank.extra_data with CPU-computed perimeter_cm
            cpu_perimeter_cm = result.get('perimeter_cm', 0)
            if cpu_perimeter_cm > 0 and marker_record.marker_id:
                marker_bank = db.query(MarkerBank).filter(
                    MarkerBank.id == marker_record.marker_id
                ).first()
                if marker_bank:
                    extra = marker_bank.extra_data or {}
                    extra["perimeter_cm"] = cpu_perimeter_cm
                    marker_bank.extra_data = extra

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
            file_type=pattern.file_type,
            quadtree_depth=quadtree_depth,
            early_termination=early_termination,
            exploration_time=int(exploration_time_s) if exploration_time_s else None,
            compression_time=int(compression_time_s) if compression_time_s else None,
            seed_screening=seed_screening,
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

            # Re-load cutplan with markers+layouts+marker_bank for cost recalculation
            db.expire_all()
            cutplan = db.query(Cutplan).options(
                joinedload(Cutplan.markers).joinedload(CutplanMarker.layout),
                joinedload(Cutplan.markers).joinedload(CutplanMarker.marker),
                joinedload(Cutplan.order),
            ).filter(Cutplan.id == cutplan_id).first()
            cutplan.status = CutplanStatus.refined

            # Recalculate costs using refined marker lengths
            _recalculate_cutplan_costs(db, cutplan, customer_id)

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
        quadtree_depth=request.quadtree_depth,
        early_termination=request.early_termination,
        exploration_time_s=request.exploration_time_s,
        compression_time_s=request.compression_time_s,
        seed_screening=request.seed_screening,
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

    for idx, cm in enumerate(cutplan_markers):
        if cm.layout:
            layouts.append(MarkerLayoutResponse(
                id=cm.layout.id,
                cutplan_marker_id=cm.id,
                marker_label=cm.marker_label or f"M{idx + 1}",
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
            label = cm.marker_label or f"M{idx + 1}"
            arcname = f"{label}_{cm.ratio_str}.dxf"
            zf.write(dxf_path, arcname)

    zip_buffer.seek(0)

    order_number = cutplan.order.order_number if cutplan.order else "unknown"
    filename = f"{order_number}_{cutplan.name or 'cutplan'}_markers.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
