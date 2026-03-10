from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, BackgroundTasks
from sqlalchemy.orm import Session, joinedload, defer
import asyncio
import json
import logging
import traceback

logger = logging.getLogger(__name__)

from ...database import get_db, SessionLocal
from ...schemas.nesting import (
    NestingJobCreate, NestingJobResponse, NestingJobListResponse, NestingJobResultResponse,
    TestMarkerRequest, TestMarkerResponse,
    TestMarkerResultResponse, TestMarkerResultListItem, TestMarkerResultUpdate,
)
from ...models import User, NestingJob, NestingJobResult, MarkerBank, Pattern, Order, TestMarkerResult
from ...services.nesting_service import NestingService
from ...config import resolve_path
from ..deps import get_current_user

router = APIRouter(prefix="/nesting", tags=["nesting"])
nesting_service = NestingService()


def execute_nesting_job(job_id: str):
    """Execute a nesting job in the background."""
    db = SessionLocal()
    try:
        job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
        if not job:
            return

        pattern = db.query(Pattern).filter(Pattern.id == job.pattern_id).first()
        if not pattern:
            job.status = "failed"
            job.error_message = "Pattern not found"
            db.commit()
            return

        # Update order status
        order = db.query(Order).filter(Order.id == job.order_id).first()
        if order:
            order.status = "nesting_in_progress"
            db.commit()

        # Run the GPU nesting
        nesting_service.run_gpu_nesting(db, job, pattern)

    except Exception as e:
        traceback.print_exc()
        job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
    finally:
        db.close()

# Store active WebSocket connections
active_connections: dict[str, WebSocket] = {}


@router.post("/jobs", response_model=NestingJobResponse, status_code=status.HTTP_201_CREATED)
async def create_nesting_job(
    job_data: NestingJobCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Submit a new GPU nesting job."""
    # Verify pattern exists and belongs to customer
    pattern = db.query(Pattern).filter(
        Pattern.id == job_data.pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    if not pattern.is_parsed:
        raise HTTPException(status_code=400, detail="Pattern must be parsed first")

    # Clamp gpu_scale to safe range (0.05 - 1.0 px/mm)
    gpu_scale = max(0.05, min(1.0, job_data.gpu_scale))

    # Validate strategy
    strategy = job_data.strategy if job_data.strategy in ("auto", "brute_force", "lhs_predict") else "auto"

    # Create job
    job = nesting_service.create_job(
        db=db,
        order_id=job_data.order_id,
        pattern_id=job_data.pattern_id,
        fabric_width_inches=job_data.fabric_width_inches,
        max_bundle_count=job_data.max_bundle_count,
        top_n_results=job_data.top_n_results,
        full_coverage=job_data.full_coverage,
        gpu_scale=gpu_scale,
        selected_sizes=job_data.selected_sizes,
        strategy=strategy,
        fabric_widths=job_data.fabric_widths,
    )

    # Update order status to pending_nesting
    order = db.query(Order).filter(Order.id == job_data.order_id).first()
    if order:
        order.status = "pending_nesting"
        db.commit()

    # Schedule the job to run in background
    background_tasks.add_task(execute_nesting_job, job.id)

    return job


@router.post("/jobs/{job_id}/run", response_model=NestingJobResponse)
async def run_nesting_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start/restart a nesting job."""
    job = db.query(NestingJob).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ["running"]:
        raise HTTPException(status_code=400, detail="Job is already running")

    # Reset job status
    job.status = "pending"
    job.progress = 0
    job.progress_message = "Queued for processing"
    job.error_message = None
    db.commit()

    # Schedule the job
    background_tasks.add_task(execute_nesting_job, job.id)

    return job


@router.get("/jobs/{job_id}", response_model=NestingJobResponse)
async def get_nesting_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get nesting job status and results."""
    job = db.query(NestingJob).options(
        joinedload(NestingJob.results)
    ).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs", response_model=List[NestingJobListResponse])
async def list_nesting_jobs(
    order_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List nesting jobs. SVG previews are excluded for performance."""
    query = db.query(NestingJob).options(
        joinedload(NestingJob.results).defer(NestingJobResult.svg_preview)
    ).join(Pattern).filter(
        Pattern.customer_id == current_user.customer_id
    )
    if order_id:
        query = query.filter(NestingJob.order_id == order_id)
    if status:
        query = query.filter(NestingJob.status == status)

    jobs = query.order_by(NestingJob.created_at.desc()).offset(skip).limit(limit).all()
    # De-duplicate from joinedload cartesian product
    seen = set()
    unique_jobs = []
    for j in jobs:
        if j.id not in seen:
            seen.add(j.id)
            unique_jobs.append(j)
    return unique_jobs


@router.delete("/jobs/{job_id}")
async def cancel_nesting_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a nesting job."""
    job = db.query(NestingJob).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ["completed", "failed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")

    # TODO: Cancel Celery task if running
    # if job.celery_task_id:
    #     celery_app.control.revoke(job.celery_task_id, terminate=True)

    job.status = "cancelled"
    job.progress_message = "Cancelling..."
    db.commit()
    return {"message": "Job cancelled"}


@router.post("/jobs/{job_id}/cancel")
async def cancel_running_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a running nesting job. The GPU runner will stop at the next checkpoint."""
    job = db.query(NestingJob).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in ["running", "pending"]:
        raise HTTPException(status_code=400, detail=f"Job is {job.status}, cannot cancel")

    job.status = "cancelled"
    job.progress_message = "Cancelling..."
    db.commit()
    return {"message": "Job cancellation requested", "status": "cancelled"}


@router.websocket("/jobs/{job_id}/stream")
async def websocket_job_progress(
    websocket: WebSocket,
    job_id: str,
    db: Session = Depends(get_db)
):
    """WebSocket endpoint for real-time job progress updates."""
    await websocket.accept()
    active_connections[job_id] = websocket

    try:
        while True:
            # Get job status
            job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
            if not job:
                await websocket.send_json({"error": "Job not found"})
                break

            # Send progress update
            await websocket.send_json({
                "job_id": job_id,
                "status": job.status,
                "progress": job.progress,
                "message": job.progress_message,
            })

            # If job is complete, send final results and close
            if job.status in ["completed", "failed", "cancelled"]:
                if job.status == "completed":
                    results = db.query(NestingJobResult).filter(
                        NestingJobResult.nesting_job_id == job_id
                    ).all()
                    await websocket.send_json({
                        "job_id": job_id,
                        "status": "completed",
                        "results": [
                            {
                                "bundle_count": r.bundle_count,
                                "rank": r.rank,
                                "ratio_str": r.ratio_str,
                                "efficiency": r.efficiency,
                                "length_yards": r.length_yards,
                            }
                            for r in results
                        ]
                    })
                break

            # Poll interval
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        pass
    finally:
        if job_id in active_connections:
            del active_connections[job_id]


@router.get("/jobs/{job_id}/preview")
async def get_job_preview(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current marker preview image for a running job."""
    job = db.query(NestingJob).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    preview = nesting_service.get_preview(job_id)
    if not preview:
        return {
            "has_preview": False,
            "ratio_str": None,
            "efficiency": None,
            "preview_base64": None,
            "timestamp": None,
        }

    return {
        "has_preview": True,
        "ratio_str": preview['ratio_str'],
        "efficiency": preview['efficiency'],
        "preview_base64": preview['preview_base64'],
        "timestamp": preview['timestamp'],
    }


@router.post("/test-marker", response_model=TestMarkerResponse)
async def test_marker(
    request: TestMarkerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Run a quick single-ratio CPU (Spyrrow) nest and persist the result."""
    import time as _time
    from ...services.spyrrow_nesting_runner import (
        load_pieces_for_spyrrow, nest_single_marker, export_marker_svg, export_marker_dxf,
    )

    # Verify pattern exists and belongs to customer
    pattern = db.query(Pattern).filter(
        Pattern.id == request.pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    if not pattern.is_parsed:
        raise HTTPException(status_code=400, detail="Pattern must be parsed first")

    if not pattern.dxf_file_path:
        raise HTTPException(status_code=400, detail="Pattern DXF file not available")
    if not pattern.rul_file_path and pattern.file_type not in ("dxf_only", "vt_dxf"):
        raise HTTPException(status_code=400, detail="Pattern RUL file not available")

    # Validate size_bundles
    total_bundles = sum(request.size_bundles.values())
    if total_bundles < 1 or total_bundles > 8:
        raise HTTPException(status_code=400, detail="Total bundles must be between 1 and 8")

    # Validate requested sizes exist in pattern
    valid_sizes = set(pattern.available_sizes)
    for size in request.size_bundles:
        if size not in valid_sizes:
            raise HTTPException(status_code=400, detail=f"Size '{size}' not available in pattern")

    # Validate parameters
    time_limit = max(1.0, min(600.0, request.time_limit))
    piece_buffer_mm = max(0.0, min(10.0, request.piece_buffer_mm))
    edge_buffer_mm = max(0.0, min(20.0, request.edge_buffer_mm))
    orientation = request.orientation if request.orientation in ("free", "nap_one_way") else "free"
    quadtree_depth = max(2, min(10, request.quadtree_depth))
    early_termination = request.early_termination

    # Validate exploration/compression time
    exploration_time_s = None
    compression_time_s = None
    if request.exploration_time_s is not None and request.compression_time_s is not None:
        exploration_time_s = max(1, min(3600, request.exploration_time_s))
        compression_time_s = max(1, min(3600, request.compression_time_s))

    # Determine material — use request material or first available
    if not pattern.available_materials:
        raise HTTPException(status_code=400, detail="Pattern has no materials")
    if request.material:
        if request.material.upper() not in [m.upper() for m in pattern.available_materials]:
            raise HTTPException(status_code=400, detail=f"Material '{request.material}' not available in pattern")
        material = request.material.upper()
    else:
        material = pattern.available_materials[0]

    # Resolve file paths
    dxf_path = resolve_path(pattern.dxf_file_path)
    rul_path = resolve_path(pattern.rul_file_path) if pattern.rul_file_path else None

    # Get full sizes list for ratio_str ordering (all sizes in pattern order)
    sizes = list(pattern.available_sizes)

    # Determine allowed rotations based on orientation
    allowed_rotations = [0, 180] if orientation == "free" else [0]

    fabric_width_mm = request.fabric_width_inches * 25.4

    use_cloud = getattr(request, 'use_cloud', False)
    seed_screening = getattr(request, 'seed_screening', False)

    try:
        t0 = _time.time()

        # Load and grade pieces (always local — parsing stays on-prem)
        nesting_pieces, piece_config = load_pieces_for_spyrrow(
            dxf_path, rul_path, material, sizes, allowed_rotations=allowed_rotations,
            file_type=pattern.file_type,
        )

        if use_cloud:
            # --- Remote path: SSH to Surface nesting worker ---
            import subprocess
            from ...services.spyrrow_nesting_runner import (
                _group_pieces_by_name, build_bundle_pieces,
            )
            from nesting_engine.core.solution import PlacedPiece

            grouped = _group_pieces_by_name(nesting_pieces)
            bundle_pieces = build_bundle_pieces(grouped, piece_config, request.size_bundles)

            if not bundle_pieces:
                raise HTTPException(status_code=400, detail="No pieces generated for this ratio")

            effective_width = fabric_width_mm - 2 * edge_buffer_mm

            # Build job payload: vertices + config (no names sent to remote)
            remote_pieces = []
            for bp in bundle_pieces:
                verts = [list(v) for v in bp.piece.vertices]
                if verts and verts[0] != verts[-1]:
                    verts.append(verts[0])
                remote_pieces.append({
                    "vertices": verts,
                    "demand": 1,
                    "allowed_orientations": [0.0, 180.0] if orientation == "free" else [0.0],
                })

            remote_config = {
                "quadtree_depth": quadtree_depth,
                "early_termination": early_termination,
                "seed": 42,
                "num_workers": 0,
                "min_items_separation": piece_buffer_mm if piece_buffer_mm > 0 else None,
            }
            if exploration_time_s is not None and compression_time_s is not None:
                remote_config["exploration_time"] = exploration_time_s
                remote_config["compression_time"] = compression_time_s
            else:
                remote_config["time_limit_s"] = int(time_limit)

            job_payload = json.dumps({
                "pieces": remote_pieces,
                "strip_width_mm": effective_width,
                "config": remote_config,
                "label": ratio_str if 'ratio_str' in dir() else "test",
            })

            # SSH pipe to Surface worker
            ssh_cmd = [
                "ssh", "-o", "ConnectTimeout=5", "surface",
                "source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py --stdin",
            ]
            logger.info(f"Dispatching to Surface: {len(remote_pieces)} pieces, width={effective_width:.0f}mm")
            proc = subprocess.run(
                ssh_cmd,
                input=job_payload,
                capture_output=True,
                text=True,
                timeout=int(time_limit) + 30,  # extra 30s for SSH overhead
            )
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Surface nesting failed: {proc.stderr.strip()[-500:]}"
                )

            cloud_result = json.loads(proc.stdout)

            # Map placements back to local bundle_pieces
            placements = []
            for cp in cloud_result["placements"]:
                idx = cp["piece_index"]
                if idx < len(bundle_pieces):
                    bp = bundle_pieces[idx]
                    placements.append(PlacedPiece(
                        piece_id=bp.piece.id,
                        instance_index=0,
                        x=cp["x"],
                        y=cp["y"],
                        rotation=cp["rotation"],
                        flipped=False,
                    ))

            class _RemoteSolution:
                def __init__(self, strip_length, util_pct, placed):
                    self.strip_length = strip_length
                    self.utilization_percent = util_pct * 100
                    self.placements = placed

            mock_solution = _RemoteSolution(
                cloud_result["strip_length_mm"],
                cloud_result["utilization"],
                placements,
            )

            solution_data = {
                'utilization': cloud_result["utilization"],
                'strip_length_mm': cloud_result["strip_length_mm"],
                'length_yards': cloud_result["strip_length_mm"] / 914.4,
                'solution': mock_solution,
                'bundle_pieces': bundle_pieces,
                'computation_time_s': cloud_result.get("computation_time_s", 0),
            }
        else:
            # --- Local path ---
            solution_data = nest_single_marker(
                ratio=request.size_bundles,
                nesting_pieces=nesting_pieces,
                piece_config=piece_config,
                fabric_width_mm=fabric_width_mm,
                piece_buffer_mm=piece_buffer_mm,
                edge_buffer_mm=edge_buffer_mm,
                time_limit=time_limit,
                quadtree_depth=quadtree_depth,
                early_termination=early_termination,
                exploration_time=exploration_time_s,
                compression_time=compression_time_s,
                seed_screening=seed_screening,
            )

        computation_time_ms = (_time.time() - t0) * 1000

        # Generate SVG preview and DXF
        svg_preview = export_marker_svg(solution_data, fabric_width_mm)
        ratio_str = '-'.join(str(request.size_bundles.get(s, 0)) for s in sizes)
        dxf_bytes = export_marker_dxf(solution_data, fabric_width_mm, ratio_str)

        piece_count = len(solution_data.get('bundle_pieces', []))

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Nesting failed: {str(e)}")

    # Auto-save to DB
    saved = TestMarkerResult(
        pattern_id=request.pattern_id,
        order_id=request.order_id,
        created_by=current_user.id,
        ratio_str=ratio_str,
        size_bundles=request.size_bundles,
        bundle_count=total_bundles,
        material=material,
        efficiency=solution_data['utilization'],
        length_mm=solution_data['strip_length_mm'],
        length_yards=solution_data['length_yards'],
        fabric_width_mm=fabric_width_mm,
        piece_count=piece_count,
        computation_time_ms=computation_time_ms,
        svg_preview=svg_preview,
        dxf_data=dxf_bytes,
        time_limit_s=time_limit,
        quadtree_depth=quadtree_depth,
        early_termination=early_termination,
        piece_buffer_mm=piece_buffer_mm,
        edge_buffer_mm=edge_buffer_mm,
        orientation=orientation,
        exploration_time_s=exploration_time_s,
        compression_time_s=compression_time_s,
        use_cloud=use_cloud,
        seed_used=solution_data.get('seed_used'),
        seed_screening=seed_screening,
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)

    return TestMarkerResponse(
        id=saved.id,
        efficiency=solution_data['utilization'],
        length_mm=solution_data['strip_length_mm'],
        length_yards=solution_data['length_yards'],
        fabric_width_mm=fabric_width_mm,
        piece_count=piece_count,
        bundle_count=total_bundles,
        ratio_str=ratio_str,
        computation_time_ms=computation_time_ms,
        svg_preview=svg_preview,
        exploration_time_s=exploration_time_s,
        compression_time_s=compression_time_s,
        use_cloud=use_cloud,
        seed_used=solution_data.get('seed_used'),
        seed_screening=seed_screening,
    )


@router.get("/test-markers", response_model=List[TestMarkerResultListItem])
async def list_test_markers(
    pattern_id: Optional[str] = None,
    order_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List saved test marker results (without SVG for performance)."""
    from sqlalchemy.orm import defer as sa_defer
    query = db.query(TestMarkerResult).options(
        sa_defer(TestMarkerResult.svg_preview),
        sa_defer(TestMarkerResult.dxf_data),
    ).filter(
        TestMarkerResult.created_by == current_user.id
    )
    if pattern_id:
        query = query.filter(TestMarkerResult.pattern_id == pattern_id)
    if order_id:
        query = query.filter(TestMarkerResult.order_id == order_id)

    results = query.order_by(TestMarkerResult.created_at.desc()).offset(skip).limit(limit).all()
    return results


@router.get("/test-markers/{result_id}", response_model=TestMarkerResultResponse)
async def get_test_marker(
    result_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a single test marker result with full SVG."""
    from sqlalchemy.orm import defer as sa_defer
    result = db.query(TestMarkerResult).options(
        sa_defer(TestMarkerResult.dxf_data),
    ).filter(
        TestMarkerResult.id == result_id,
        TestMarkerResult.created_by == current_user.id
    ).first()
    if not result:
        raise HTTPException(status_code=404, detail="Test marker result not found")
    return result


@router.delete("/test-markers/{result_id}")
async def delete_test_marker(
    result_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a saved test marker result."""
    result = db.query(TestMarkerResult).filter(
        TestMarkerResult.id == result_id,
        TestMarkerResult.created_by == current_user.id
    ).first()
    if not result:
        raise HTTPException(status_code=404, detail="Test marker result not found")
    db.delete(result)
    db.commit()
    return {"message": "Deleted"}


@router.patch("/test-markers/{result_id}", response_model=TestMarkerResultListItem)
async def update_test_marker(
    result_id: str,
    update: TestMarkerResultUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update notes on a saved test marker result."""
    result = db.query(TestMarkerResult).filter(
        TestMarkerResult.id == result_id,
        TestMarkerResult.created_by == current_user.id
    ).first()
    if not result:
        raise HTTPException(status_code=404, detail="Test marker result not found")
    if update.notes is not None:
        result.notes = update.notes
    db.commit()
    db.refresh(result)
    return result


@router.get("/markers", response_model=List[dict])
async def list_markers(
    pattern_id: Optional[str] = None,
    fabric_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List markers from marker bank."""
    query = db.query(MarkerBank).join(Pattern).filter(
        Pattern.customer_id == current_user.customer_id
    )
    if pattern_id:
        query = query.filter(MarkerBank.pattern_id == pattern_id)
    if fabric_id:
        query = query.filter(MarkerBank.fabric_id == fabric_id)

    markers = query.offset(skip).limit(limit).all()
    return [
        {
            "id": m.id,
            "pattern_id": m.pattern_id,
            "fabric_id": m.fabric_id,
            "ratio_str": m.ratio_str,
            "efficiency": m.efficiency,
            "length_yards": m.length_yards,
            "source_type": m.source_type,
        }
        for m in markers
    ]
