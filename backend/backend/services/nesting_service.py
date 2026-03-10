import os
import sys
import base64
import time
from typing import Optional, List, Dict, Any, Callable
from sqlalchemy.orm import Session

from ..config import settings
from ..models import NestingJob, NestingJobResult, Pattern, MarkerBank, Order, Fabric

# In-memory storage for current preview (cleared on job completion)
# Key: job_id, Value: {preview_base64, ratio_str, efficiency, timestamp}
_preview_cache: Dict[str, Dict] = {}


class NestingService:
    """Service for GPU nesting job management."""

    def set_preview(
        self,
        job_id: str,
        preview_base64: str,
        ratio_str: str,
        efficiency: float
    ):
        """Store current marker preview for a job."""
        _preview_cache[job_id] = {
            'preview_base64': preview_base64,
            'ratio_str': ratio_str,
            'efficiency': efficiency,
            'timestamp': time.time()
        }

    def get_preview(self, job_id: str) -> Optional[Dict]:
        """Get current marker preview for a job."""
        return _preview_cache.get(job_id)

    def clear_preview(self, job_id: str):
        """Clear preview cache for a job."""
        if job_id in _preview_cache:
            del _preview_cache[job_id]

    def create_job(
        self,
        db: Session,
        order_id: str,
        pattern_id: str,
        fabric_width_inches: float,
        max_bundle_count: int = 6,
        top_n_results: int = 10,
        full_coverage: bool = False,
        gpu_scale: float = 0.15,
        selected_sizes: list = None,
        strategy: str = "auto",
        fabric_widths: list = None,
    ) -> NestingJob:
        """Create a new nesting job."""
        job = NestingJob(
            order_id=order_id,
            pattern_id=pattern_id,
            fabric_width_inches=fabric_width_inches,
            fabric_widths=fabric_widths,
            max_bundle_count=max_bundle_count,
            top_n_results=top_n_results,
            full_coverage=full_coverage,
            gpu_scale=gpu_scale,
            selected_sizes=selected_sizes,
            strategy=strategy,
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
        length_mm: Optional[float] = None,
        fabric_width_inches: Optional[float] = None,
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
            fabric_width_inches=fabric_width_inches,
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
        metadata: Optional[Dict] = None,
        fabric_width_inches: Optional[float] = None,
    ) -> MarkerBank:
        """Add a marker to the marker bank."""
        # Check if marker already exists (dedup includes fabric width)
        query = db.query(MarkerBank).filter(
            MarkerBank.pattern_id == pattern_id,
            MarkerBank.fabric_id == fabric_id,
            MarkerBank.ratio_str == ratio_str,
        )
        if fabric_width_inches is not None:
            query = query.filter(MarkerBank.fabric_width_inches == fabric_width_inches)
        else:
            query = query.filter(MarkerBank.fabric_width_inches.is_(None))
        existing = query.first()

        if existing:
            # Update if new efficiency is better
            if efficiency > existing.efficiency:
                existing.efficiency = efficiency
                existing.length_yards = length_yards
                existing.length_mm = length_mm
                existing.source_type = source_type
                existing.extra_data = metadata or {}
                db.commit()
                db.refresh(existing)
            elif metadata and metadata.get("perimeter_cm"):
                # Even if efficiency isn't better, update perimeter_cm if we have it
                existing_data = existing.extra_data or {}
                if not existing_data.get("perimeter_cm"):
                    existing_data["perimeter_cm"] = metadata["perimeter_cm"]
                    existing.extra_data = existing_data
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
            fabric_width_inches=fabric_width_inches,
            source_type=source_type,
            extra_data=metadata or {},
        )
        db.add(marker)
        db.commit()
        db.refresh(marker)
        return marker

    def get_material_for_job(
        self,
        db: Session,
        job: NestingJob,
    ) -> str:
        """Get the primary material to nest for a job."""
        # Get the order to find which fabric/material we're nesting
        order = db.query(Order).filter(Order.id == job.order_id).first()
        if not order or not order.order_lines:
            raise ValueError("Order not found or has no order lines")

        # Get the first order line's fabric code as the material
        first_line = order.order_lines[0]
        return first_line.fabric_code

    def get_fabric_for_job(
        self,
        db: Session,
        job: NestingJob,
    ) -> Optional[Fabric]:
        """Get the fabric record for a job."""
        order = db.query(Order).filter(Order.id == job.order_id).first()
        if not order or not order.order_lines:
            return None

        first_line = order.order_lines[0]
        if first_line.fabric_id:
            return db.query(Fabric).filter(Fabric.id == first_line.fabric_id).first()

        # Try to find by code
        return db.query(Fabric).filter(
            Fabric.code == first_line.fabric_code
        ).first()

    def run_gpu_nesting(
        self,
        db: Session,
        job: NestingJob,
        pattern: Pattern,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> List[NestingJobResult]:
        """
        Run GPU nesting algorithm.
        This is the main entry point that will be called by the job runner.
        """
        from .gpu_nesting_runner import run_nesting_for_material, NestingCancelled, generate_all_ratios

        results = []

        try:
            # Update job status
            job.status = "running"
            db.commit()

            # Get material and fabric for this job
            material = self.get_material_for_job(db, job)
            fabric = self.get_fabric_for_job(db, job)

            if not fabric:
                raise ValueError(f"No fabric record found for material {material}")

            # Get pattern file paths (convert relative to absolute)
            if not pattern.dxf_file_path:
                raise ValueError("Pattern DXF file not available")
            if not pattern.rul_file_path and pattern.file_type not in ("dxf_only", "vt_dxf"):
                raise ValueError("Pattern RUL file not available")

            from ..config import resolve_path
            dxf_path = resolve_path(pattern.dxf_file_path)
            rul_path = resolve_path(pattern.rul_file_path) if pattern.rul_file_path else None

            # Get sizes: use selected_sizes from job if provided, otherwise all pattern sizes
            # Strip whitespace from size strings to accept all non-empty sizes
            if job.selected_sizes:
                sizes = [s.strip() for s in job.selected_sizes if s and s.strip() and s.strip() in [sz.strip() for sz in pattern.available_sizes]]
            else:
                sizes = [s.strip() for s in pattern.available_sizes if s and s.strip()]
            if not sizes:
                raise ValueError("Pattern has no valid sizes")

            # Progress wrapper
            def update_progress(progress: int, message: str):
                job.progress = progress
                job.progress_message = message
                db.commit()
                if progress_callback:
                    progress_callback(progress, message)

            # Preview callback - store in cache for frontend polling
            def update_preview(ratio_str: str, preview_base64: str, efficiency: float):
                self.set_preview(job.id, preview_base64, ratio_str, efficiency)

            # Result callback - save results incrementally as each bundle_count completes
            # (only for base width during nesting; extra widths saved in post-processing)
            def save_incremental_results(bundle_count: int, bundle_results: list):
                for rank, result in enumerate(bundle_results, 1):
                    self.save_result(
                        db=db,
                        job_id=job.id,
                        bundle_count=bundle_count,
                        rank=rank,
                        ratio_str=result['ratio_str'],
                        efficiency=result['efficiency'],
                        length_yards=result['length_yards'],
                        fabric_width_inches=job.fabric_width_inches,
                    )
                    results.append(result)
                db.commit()  # Commit so frontend can see results immediately

            # Cancel check - reads job status from DB
            def check_cancelled() -> bool:
                db.refresh(job)
                return job.status == "cancelled"

            gpu_scale = job.gpu_scale or 0.15
            update_progress(5, f"Starting GPU nesting for {material} (scale={gpu_scale} px/mm)...")

            # Run GPU nesting with callbacks for preview and incremental results
            nesting_results = run_nesting_for_material(
                dxf_path=dxf_path,
                rul_path=rul_path,
                material=material,
                sizes=sizes,
                fabric_width_inches=job.fabric_width_inches,
                max_bundle_count=job.max_bundle_count,
                top_n=job.top_n_results,
                gpu_scale=gpu_scale,
                progress_callback=update_progress,
                preview_callback=update_preview,
                preview_interval_seconds=0.5,
                full_coverage=job.full_coverage or False,
                result_callback=save_incremental_results,
                cancel_check=check_cancelled,
                file_type=pattern.file_type,
                nesting_strategy=job.strategy or "auto",
                fabric_widths=job.fabric_widths,
            )

            # nesting_results is now {width_inches: {bc: [results]}}
            # Base width results were saved incrementally via callback.
            # Now add to marker bank and save extra-width results.
            for width_inches, width_results in nesting_results.items():
                is_base = (width_inches == job.fabric_width_inches) or (
                    not job.fabric_widths or len(job.fabric_widths) <= 1
                )

                for bundle_count, bundle_results in width_results.items():
                    for rank, result in enumerate(bundle_results, 1):
                        # Save extra-width results to DB (base already saved incrementally)
                        if not is_base:
                            self.save_result(
                                db=db,
                                job_id=job.id,
                                bundle_count=bundle_count,
                                rank=rank,
                                ratio_str=result['ratio_str'],
                                efficiency=result['efficiency'],
                                length_yards=result['length_yards'],
                                fabric_width_inches=width_inches,
                            )
                            results.append(result)

                        # Add to marker bank
                        self.add_to_marker_bank(
                            db=db,
                            pattern_id=pattern.id,
                            fabric_id=fabric.id,
                            ratio_str=result['ratio_str'],
                            efficiency=result['efficiency'],
                            length_yards=result['length_yards'],
                            source_type="gpu_nesting",
                            metadata={
                                "bundle_count": bundle_count,
                                "job_id": job.id,
                                "perimeter_cm": result.get('perimeter_cm', 0),
                            },
                            fabric_width_inches=width_inches,
                        )

                        # Update SVG preview on the DB result record (base width only)
                        if is_base:
                            svg_preview = result.get('svg_preview')
                            if svg_preview:
                                db_result = db.query(NestingJobResult).filter(
                                    NestingJobResult.nesting_job_id == job.id,
                                    NestingJobResult.ratio_str == result['ratio_str'],
                                    NestingJobResult.fabric_width_inches == width_inches,
                                ).first()
                                if db_result:
                                    db_result.svg_preview = svg_preview

            # Mark job complete
            job.status = "completed"
            job.progress = 100
            total_evaluated = sum(len(generate_all_ratios(bc, sizes)) for bc in range(1, (job.max_bundle_count or 6) + 1))
            job.progress_message = f"Completed — {total_evaluated} ratios evaluated, {len(results)} markers retained"
            db.commit()

            # Clear preview cache
            self.clear_preview(job.id)

            # Update order status
            order = db.query(Order).filter(Order.id == job.order_id).first()
            if order and order.status in ("pending_nesting", "nesting_in_progress"):
                order.status = "pending_cutplan"
                db.commit()

        except NestingCancelled:
            # Cancelled by user — keep status as "cancelled" (already set by the cancel endpoint)
            job.progress_message = f"Cancelled by user — {len(results)} markers saved"
            self.clear_preview(job.id)
            db.commit()
            # Don't raise — this is a graceful stop

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            # Clear preview cache on failure too
            self.clear_preview(job.id)
            db.commit()
            raise

        return results


# Singleton instance
nesting_service = NestingService()
