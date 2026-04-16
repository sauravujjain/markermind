"""
Global Surface Nesting Queue — ensures only 1 Surface nest runs at a time.

Multiple refine threads submit jobs; each blocks until its job completes.
The queue processes one job at a time, preventing CPU contention on Surface.
"""
import json
import logging
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """Raised when a queued job is cancelled before it starts."""


@dataclass
class SurfaceNestJob:
    id: str
    params: Dict[str, Any]       # kwargs for nest_single_marker_surface
    marker_label: str
    cutplan_id: str
    status: str = "queued"       # queued | running | completed | failed | cancelled
    result: Optional[Dict] = None
    error: Optional[str] = None
    event: threading.Event = field(default_factory=threading.Event)
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    time_limit_override: Optional[float] = None  # per-job override


class SurfaceNestingQueue:
    """Global singleton — ensures only 1 Surface nest runs at a time."""

    _instance: ClassVar[Optional['SurfaceNestingQueue']] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def get(cls) -> 'SurfaceNestingQueue':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._queue: queue.Queue[SurfaceNestJob] = queue.Queue()
        self._jobs: Dict[str, SurfaceNestJob] = {}
        self._current: Optional[SurfaceNestJob] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancel_flag = False
        self._paused = threading.Event()
        self._paused.set()  # starts unpaused
        self._completed_jobs: List[SurfaceNestJob] = []
        self._max_completed = 50
        self._worker = threading.Thread(target=self._run, daemon=True, name="surface-queue-worker")
        self._worker.start()
        logger.info("SurfaceNestingQueue initialized — worker thread started")

    def submit(self, params: Dict[str, Any], marker_label: str, cutplan_id: str) -> Dict:
        """Submit a job and BLOCK until complete. Returns nest result dict."""
        job = SurfaceNestJob(
            id=str(uuid.uuid4())[:8],
            params=params,
            marker_label=marker_label,
            cutplan_id=cutplan_id,
        )
        self._jobs[job.id] = job
        self._queue.put(job)
        logger.info(f"Queue: submitted {job.id} ({marker_label}) for cutplan {cutplan_id} — "
                     f"queue depth={self._queue.qsize()}")

        # Block until done
        job.event.wait()

        if job.status == "cancelled":
            raise CancelledError(f"Job {job.id} was cancelled")
        if job.error:
            raise RuntimeError(f"Surface nest failed for {marker_label}: {job.error}")
        return job.result

    def status(self) -> Dict:
        """Return queue snapshot for dashboard."""
        # Use _jobs dict as source of truth — thread-safe reads
        queued = []
        for j in list(self._jobs.values()):
            if j.status == "queued":
                queued.append(_job_summary(j))
        # Sort by submission time
        queued.sort(key=lambda x: x["submitted_at"])

        current = _job_summary(self._current) if self._current else None

        completed = [_job_summary(j) for j in reversed(self._completed_jobs)]

        return {
            "paused": not self._paused.is_set(),
            "current": current,
            "queued": queued,
            "queued_count": len(queued),
            "completed": completed,
            "completed_count": len(completed),
            "total_compute_s": sum(
                (j.completed_at - j.started_at)
                for j in self._completed_jobs
                if j.started_at and j.completed_at
            ),
        }

    def kill_current(self) -> bool:
        """Kill the running SSH process. Worker catches it and moves on."""
        if self._current and self._proc:
            logger.warning(f"Queue: killing current job {self._current.id} ({self._current.marker_label})")
            self._cancel_flag = True
            try:
                self._proc.terminate()
            except Exception:
                pass
            return True
        return False

    def remove_job(self, job_id: str) -> bool:
        """Remove a queued (not running) job. Unblocks waiting thread."""
        job = self._jobs.get(job_id)
        if not job or job.status != "queued":
            return False

        # Drain queue, skip target, re-add rest
        temp = []
        found = False
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item.id == job_id:
                    found = True
                    item.status = "cancelled"
                    item.completed_at = time.time()
                    item.event.set()
                    self._completed_jobs.append(item)
                    self._prune_completed()
                else:
                    temp.append(item)
            except queue.Empty:
                break

        for item in temp:
            self._queue.put(item)

        logger.info(f"Queue: removed job {job_id} (found={found})")
        return found

    def clear_queue(self):
        """Kill current + cancel all queued."""
        # Cancel all queued
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                item.status = "cancelled"
                item.completed_at = time.time()
                item.event.set()
                self._completed_jobs.append(item)
            except queue.Empty:
                break

        self._prune_completed()

        # Kill current
        self.kill_current()
        logger.info("Queue: cleared all jobs")

    def pause(self):
        """Pause processing — finish current job but don't start next."""
        self._paused.clear()
        logger.info("Queue: paused")

    def resume(self):
        """Resume processing."""
        self._paused.set()
        logger.info("Queue: resumed")

    def prioritize(self, job_id: str) -> bool:
        """Move a queued job to front of queue."""
        temp = []
        target = None
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item.id == job_id:
                    target = item
                else:
                    temp.append(item)
            except queue.Empty:
                break

        if target:
            self._queue.put(target)

        for item in temp:
            self._queue.put(item)

        if target:
            logger.info(f"Queue: prioritized {job_id}")
        return target is not None

    def jobs_by_cutplan(self) -> Dict[str, List[SurfaceNestJob]]:
        """Group all active jobs (queued + running) by cutplan_id."""
        groups: Dict[str, List[SurfaceNestJob]] = {}
        for job in list(self._jobs.values()):
            if job.status in ("queued", "running"):
                groups.setdefault(job.cutplan_id, []).append(job)
        # Sort each group by submission time
        for jobs in groups.values():
            jobs.sort(key=lambda j: j.submitted_at)
        return groups

    def update_time_limit(self, job_id: str, time_limit_s: float) -> bool:
        """Update time limit for a queued (not yet running) job."""
        job = self._jobs.get(job_id)
        if not job or job.status != "queued":
            return False
        job.time_limit_override = time_limit_s
        logger.info(f"Queue: updated time limit for {job_id} to {time_limit_s}s")
        return True

    def _run(self):
        """Worker loop — process one job at a time."""
        while True:
            # Block if paused
            self._paused.wait()

            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Skip cancelled jobs
            if job.status == "cancelled":
                continue

            self._current = job
            job.status = "running"
            job.started_at = time.time()
            self._cancel_flag = False

            # Set performance mode when first job starts
            _set_performance_mode(True)

            try:
                result = self._run_surface_nest(job)
                if self._cancel_flag:
                    job.status = "cancelled"
                    job.error = "Killed by user"
                else:
                    job.result = result
                    job.status = "completed"
            except Exception as e:
                if self._cancel_flag:
                    job.status = "cancelled"
                    job.error = "Killed by user"
                else:
                    job.status = "failed"
                    job.error = str(e)
                    logger.error(f"Queue: job {job.id} failed: {e}")
            finally:
                job.completed_at = time.time()
                job.event.set()
                self._completed_jobs.append(job)
                self._prune_completed()
                self._current = None
                self._proc = None
                self._cancel_flag = False

                elapsed = (job.completed_at - job.started_at) if job.started_at else 0
                logger.info(f"Queue: job {job.id} ({job.marker_label}) {job.status} "
                            f"in {elapsed:.1f}s — remaining={self._queue.qsize()}")

                # Revert performance mode if queue is empty
                if self._queue.empty():
                    _set_performance_mode(False)

    def _run_surface_nest(self, job: SurfaceNestJob) -> Dict:
        """Execute the SSH call to Surface, using Popen for killability."""
        from nesting_engine.core.solution import PlacedPiece

        params = job.params
        bundle_pieces = params["bundle_pieces"]
        effective_width = params["effective_width"]

        # Apply time limit override if set
        remote_config = dict(params["remote_config"])
        if job.time_limit_override is not None:
            # Override the time limit
            remote_config.pop("time_limit_s", None)
            remote_config.pop("exploration_time", None)
            remote_config.pop("compression_time", None)
            remote_config["time_limit_s"] = int(job.time_limit_override)

        job_payload = json.dumps({
            "pieces": params["remote_pieces"],
            "strip_width_mm": effective_width,
            "config": remote_config,
            "label": params.get("label", job.marker_label),
        })

        ssh_cmd = [
            "ssh",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=240",
            "surface",
            "source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py --stdin",
        ]

        # Compute timeout from config
        tl = remote_config.get("time_limit_s", 0)
        exp = remote_config.get("exploration_time", 0)
        comp = remote_config.get("compression_time", 0)
        timeout_s = max(tl, exp + comp) * 2 + 600

        logger.info(f"Queue: running {job.id} ({job.marker_label}) — "
                     f"{len(params['remote_pieces'])} pieces, width={effective_width:.0f}mm")

        self._proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = self._proc.communicate(input=job_payload, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.communicate()
            raise RuntimeError(f"SSH timeout after {timeout_s}s")

        if self._cancel_flag:
            raise CancelledError("Killed by user")

        if self._proc.returncode != 0:
            raise RuntimeError(f"Surface nesting failed: {stderr.strip()[-500:]}")

        result = json.loads(stdout)

        # Map placements back to local bundle_pieces
        placements = []
        for cp in result["placements"]:
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
            result["strip_length_mm"],
            result["utilization"],
            placements,
        )

        return {
            'utilization': result["utilization"],
            'strip_length_mm': result["strip_length_mm"],
            'length_yards': result["strip_length_mm"] / 914.4,
            'perimeter_cm': result.get("perimeter_cm", 0.0),
            'solution': mock_solution,
            'bundle_pieces': bundle_pieces,
            'computation_time_s': result.get("computation_time_s", 0),
        }

    def _prune_completed(self):
        """Keep only the last N completed jobs."""
        if len(self._completed_jobs) > self._max_completed:
            self._completed_jobs = self._completed_jobs[-self._max_completed:]


def _job_summary(job: Optional[SurfaceNestJob]) -> Optional[Dict]:
    """Serialize a job for the status endpoint."""
    if job is None:
        return None
    now = time.time()
    elapsed = (now - job.started_at) if job.started_at else 0
    duration = ((job.completed_at or now) - job.started_at) if job.started_at else 0

    # Estimate time limit from params
    rc = job.params.get("remote_config", {})
    tl = rc.get("time_limit_s", 0)
    exp = rc.get("exploration_time", 0)
    comp = rc.get("compression_time", 0)
    estimated_time = job.time_limit_override or max(tl, exp + comp) or 2400

    return {
        "id": job.id,
        "marker_label": job.marker_label,
        "cutplan_id": job.cutplan_id,
        "status": job.status,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "elapsed_s": elapsed if job.status == "running" else duration,
        "estimated_time_s": estimated_time,
        "time_limit_override": job.time_limit_override,
        "utilization": job.result.get("utilization") if job.result else None,
        "ratio_str": job.params.get("label", ""),
    }


def _set_performance_mode(on: bool):
    """Set Surface CPU governor to performance or powersave."""
    mode = "performance" if on else "powersave"
    try:
        # Try cpufreq first
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "surface",
             f"echo {mode} | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info(f"Surface CPU governor set to {mode}")
            return

        # Fallback: powerprofilesctl
        profile = "performance" if on else "power-saver"
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "surface",
             f"powerprofilesctl set {profile}"],
            capture_output=True, text=True, timeout=10,
        )
        logger.info(f"Surface power profile set to {profile}")
    except Exception as e:
        logger.warning(f"Failed to set performance mode: {e}")
