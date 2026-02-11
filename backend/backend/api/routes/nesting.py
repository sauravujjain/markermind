from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, BackgroundTasks
from sqlalchemy.orm import Session
import asyncio
import json
import traceback

from ...database import get_db, SessionLocal
from ...schemas.nesting import NestingJobCreate, NestingJobResponse, NestingJobResultResponse
from ...models import User, NestingJob, NestingJobResult, MarkerBank, Pattern, Order
from ...services.nesting_service import NestingService
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

    # Create job
    job = nesting_service.create_job(
        db=db,
        order_id=job_data.order_id,
        pattern_id=job_data.pattern_id,
        fabric_width_inches=job_data.fabric_width_inches,
        max_bundle_count=job_data.max_bundle_count,
        top_n_results=job_data.top_n_results,
        full_coverage=job_data.full_coverage,
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
    job = db.query(NestingJob).join(Pattern).filter(
        NestingJob.id == job_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs", response_model=List[NestingJobResponse])
async def list_nesting_jobs(
    order_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List nesting jobs."""
    query = db.query(NestingJob).join(Pattern).filter(
        Pattern.customer_id == current_user.customer_id
    )
    if order_id:
        query = query.filter(NestingJob.order_id == order_id)
    if status:
        query = query.filter(NestingJob.status == status)

    jobs = query.order_by(NestingJob.created_at.desc()).offset(skip).limit(limit).all()
    return jobs


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
    db.commit()
    return {"message": "Job cancelled"}


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
