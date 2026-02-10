import os
import sys
from typing import Optional, List, Dict, Any, Callable
from sqlalchemy.orm import Session

from ..config import settings
from ..models import NestingJob, NestingJobResult, Pattern, MarkerBank

# Add scripts to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))


class NestingService:
    """Service for GPU nesting job management."""

    def create_job(
        self,
        db: Session,
        order_id: str,
        pattern_id: str,
        fabric_width_inches: float,
        max_bundle_count: int = 6,
        top_n_results: int = 10
    ) -> NestingJob:
        """Create a new nesting job."""
        job = NestingJob(
            order_id=order_id,
            pattern_id=pattern_id,
            fabric_width_inches=fabric_width_inches,
            max_bundle_count=max_bundle_count,
            top_n_results=top_n_results,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def update_job_progress(
        self,
        db: Session,
        job_id: str,
        progress: int,
        message: str,
        status: Optional[str] = None
    ):
        """Update job progress."""
        job = db.query(NestingJob).filter(NestingJob.id == job_id).first()
        if job:
            job.progress = progress
            job.progress_message = message
            if status:
                job.status = status
            db.commit()

    def save_result(
        self,
        db: Session,
        job_id: str,
        bundle_count: int,
        rank: int,
        ratio_str: str,
        efficiency: float,
        length_yards: float,
        length_mm: Optional[float] = None
    ) -> NestingJobResult:
        """Save a nesting job result."""
        result = NestingJobResult(
            nesting_job_id=job_id,
            bundle_count=bundle_count,
            rank=rank,
            ratio_str=ratio_str,
            efficiency=efficiency,
            length_yards=length_yards,
            length_mm=length_mm,
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result

    def add_to_marker_bank(
        self,
        db: Session,
        pattern_id: str,
        fabric_id: str,
        ratio_str: str,
        efficiency: float,
        length_yards: float,
        length_mm: Optional[float] = None,
        source_type: str = "gpu_nesting",
        metadata: Optional[Dict] = None
    ) -> MarkerBank:
        """Add a marker to the marker bank."""
        # Check if marker already exists
        existing = db.query(MarkerBank).filter(
            MarkerBank.pattern_id == pattern_id,
            MarkerBank.fabric_id == fabric_id,
            MarkerBank.ratio_str == ratio_str
        ).first()

        if existing:
            # Update if new efficiency is better
            if efficiency > existing.efficiency:
                existing.efficiency = efficiency
                existing.length_yards = length_yards
                existing.length_mm = length_mm
                existing.source_type = source_type
                existing.metadata = metadata or {}
                db.commit()
                db.refresh(existing)
            return existing

        marker = MarkerBank(
            pattern_id=pattern_id,
            fabric_id=fabric_id,
            ratio_str=ratio_str,
            efficiency=efficiency,
            length_yards=length_yards,
            length_mm=length_mm,
            source_type=source_type,
            metadata=metadata or {},
        )
        db.add(marker)
        db.commit()
        db.refresh(marker)
        return marker

    def run_gpu_nesting(
        self,
        db: Session,
        job: NestingJob,
        pattern: Pattern,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> List[NestingJobResult]:
        """
        Run GPU nesting algorithm.
        This is the main entry point that will be called by Celery worker.
        """
        results = []

        try:
            # Import GPU nesting components
            # This would import from scripts/gpu_20260118_ga_ratio_optimizer.py
            # For now, this is a placeholder that would be filled in

            # Update job status
            job.status = "running"
            db.commit()

            # For each bundle count 1 to max_bundle_count
            for bundle_count in range(1, job.max_bundle_count + 1):
                if progress_callback:
                    progress = int((bundle_count - 1) / job.max_bundle_count * 100)
                    progress_callback(progress, f"Processing {bundle_count}-bundle markers...")

                # TODO: Call actual GPU nesting algorithm
                # This would:
                # 1. Load and rasterize pieces from pattern
                # 2. Generate ratio combinations for bundle_count
                # 3. Evaluate each ratio using GPU FFT convolution
                # 4. Sort by efficiency and keep top N

                # Placeholder: would be replaced with actual implementation
                pass

            # Mark job complete
            job.status = "completed"
            job.progress = 100
            job.progress_message = "Completed successfully"
            db.commit()

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
            raise

        return results
