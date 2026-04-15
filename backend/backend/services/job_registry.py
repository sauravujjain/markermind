"""
Centralized in-memory job tracking for nesting activity dashboard.

Extracted from cutplans.py to avoid circular imports when the activity
endpoint needs to read job state from multiple modules.
"""
from typing import Dict

# Key: order_id, Value: {status, progress, message, started_at, strategies_total, strategies_done, phase}
cutplan_jobs: Dict[str, Dict] = {}

# Key: cutplan_id, Value: {status, progress, message, markers_total, markers_done, started_at, ...}
refinement_jobs: Dict[str, Dict] = {}
