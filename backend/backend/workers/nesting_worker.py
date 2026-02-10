from . import celery_app
from ..database import SessionLocal
from ..models import NestingJob, Pattern
from ..services.nesting_service import NestingService
import json

nesting_service = NestingService()


@celery_app.task(bind=True)
def run_gpu_nesting_task(self, job_id: str):
    """
    Celery task for running GPU nesting.
    This task wraps the GPU nesting algorithm and provides progress updates.
    """
    db = SessionLocal()
    try:
        # Get job and pattern
        job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
        if not job:
            return {"error": "Job not found"}

        pattern = db.query(Pattern).filter(Pattern.id == job.pattern_id).first()
        if not pattern:
            return {"error": "Pattern not found"}

        # Define progress callback
        def progress_callback(progress: int, message: str):
            self.update_state(
                state="PROGRESS",
                meta={"progress": progress, "message": message}
            )
            nesting_service.update_job_progress(db, job_id, progress, message)

        # Run nesting
        results = nesting_service.run_gpu_nesting(
            db=db,
            job=job,
            pattern=pattern,
            progress_callback=progress_callback,
        )

        return {
            "status": "completed",
            "results_count": len(results),
        }

    except Exception as e:
        # Mark job as failed
        job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
        return {"error": str(e)}

    finally:
        db.close()


@celery_app.task
def cleanup_old_jobs():
    """Periodic task to clean up old completed/failed jobs."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old_jobs = db.query(NestingJob).filter(
            NestingJob.status.in_(["completed", "failed", "cancelled"]),
            NestingJob.created_at < cutoff
        ).all()

        for job in old_jobs:
            db.delete(job)

        db.commit()
        return {"cleaned": len(old_jobs)}

    finally:
        db.close()
