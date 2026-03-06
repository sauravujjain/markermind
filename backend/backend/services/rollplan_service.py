"""
Roll Plan service layer — DB orchestration for the rollplan simulator.

Handles:
  - Creating roll plans
  - Uploading/parsing roll Excel
  - Extracting MarkerSpec from CutplanMarker
  - Preparing rolls (real + pseudo fill)
  - Running simulations (MC + GA)
  - Persisting results + building response objects
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.cutplan import CutplanMarker
from ..models.rollplan import (
    FabricRoll,
    RollInputType,
    RollPlan,
    RollPlanMode,
    RollPlanStatus,
)
from .rollplan_simulator import (
    GAResult,
    MarkerSpec,
    MonteCarloResult,
    PseudoRollConfig,
    RollSpec,
    generate_pseudo_rolls,
    optimize_rolls_ga,
    parse_roll_excel,
    simulate_roll_usage,
)


class RollPlanService:

    # ------------------------------------------------------------------
    # Marker extraction from cutplan
    # ------------------------------------------------------------------

    @staticmethod
    def get_markers_for_simulation(
        db: Session,
        cutplan_id: str,
        color_code: Optional[str] = None,
    ) -> List[MarkerSpec]:
        """
        Extract MarkerSpec list from CutplanMarker rows.

        Each CutplanMarker has plies_by_color (JSON: {"NAVY": 50, "BLACK": 30}).
        If color_code is given, use that color's plies; else sum all colors.
        """
        markers_db = (
            db.query(CutplanMarker)
            .filter(CutplanMarker.cutplan_id == cutplan_id)
            .all()
        )
        result: List[MarkerSpec] = []
        for m in markers_db:
            pbc = m.plies_by_color or {}
            if color_code:
                plies = pbc.get(color_code, 0)
            else:
                plies = sum(pbc.values()) if pbc else (m.total_plies or 0)

            if plies <= 0:
                continue

            # Use refined length if available, else ILP/GPU length
            length = m.length_yards or 0
            if m.layout and m.layout.length_yards:
                length = m.layout.length_yards

            result.append(MarkerSpec(
                marker_label=m.marker_label or f"M{len(result) + 1}",
                length_yards=length,
                plies=plies,
                ratio_str=m.ratio_str or "",
            ))
        return result

    # ------------------------------------------------------------------
    # Roll data management
    # ------------------------------------------------------------------

    @staticmethod
    def upload_rolls(
        db: Session,
        roll_plan_id: str,
        file_bytes: bytes,
    ) -> List[FabricRoll]:
        """Parse roll Excel and persist FabricRoll records."""
        roll_specs = parse_roll_excel(file_bytes)
        roll_plan = db.query(RollPlan).filter(RollPlan.id == roll_plan_id).first()
        if not roll_plan:
            raise ValueError("Roll plan not found")

        # Delete any existing rolls for this plan
        db.query(FabricRoll).filter(FabricRoll.roll_plan_id == roll_plan_id).delete()

        records: List[FabricRoll] = []
        for spec in roll_specs:
            rec = FabricRoll(
                roll_plan_id=roll_plan_id,
                roll_number=spec.roll_id,
                length_yards=spec.length_yards,
                is_pseudo=False,
                width_inches=spec.width_inches,
                shrinkage_x_pct=spec.shrinkage_x_pct,
                shrinkage_y_pct=spec.shrinkage_y_pct,
                shade_group=spec.shade_group,
            )
            db.add(rec)
            records.append(rec)

        # Update input type
        roll_plan.input_type = RollInputType.real
        db.commit()

        for rec in records:
            db.refresh(rec)
        return records

    @staticmethod
    def prepare_rolls(
        db: Session,
        roll_plan: RollPlan,
        total_fabric_needed: float,
    ) -> List[RollSpec]:
        """
        Load real rolls from DB and auto-generate pseudo-rolls if shortfall.

        Returns combined list of RollSpec for the simulator.
        """
        real_rolls_db = (
            db.query(FabricRoll)
            .filter(FabricRoll.roll_plan_id == roll_plan.id, FabricRoll.is_pseudo == False)
            .all()
        )
        real_rolls = [
            RollSpec(
                roll_id=r.roll_number,
                length_yards=r.length_yards,
                is_pseudo=False,
                width_inches=r.width_inches,
                shrinkage_x_pct=r.shrinkage_x_pct,
                shrinkage_y_pct=r.shrinkage_y_pct,
                shade_group=r.shade_group,
            )
            for r in real_rolls_db
        ]

        config = PseudoRollConfig(
            avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
            delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
        )

        if not real_rolls:
            roll_plan.input_type = RollInputType.pseudo
            return None  # Signal to use pseudo-rolls only (regenerated per MC run)

        real_total = sum(r.length_yards for r in real_rolls)
        target = total_fabric_needed * 1.05

        if real_total >= target:
            roll_plan.input_type = RollInputType.real
            return real_rolls
        else:
            roll_plan.input_type = RollInputType.mixed
            pseudo = generate_pseudo_rolls(total_fabric_needed, config, real_rolls)
            return real_rolls + pseudo

    # ------------------------------------------------------------------
    # Run simulation
    # ------------------------------------------------------------------

    def run_simulation(
        self,
        db: Session,
        roll_plan: RollPlan,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        ga_pop_size: int = 30,
        ga_generations: int = 50,
    ) -> None:
        """
        Orchestrate MC + GA simulation, save results to roll_plan.

        Updates roll_plan in place. Caller should commit after.
        """
        markers = self.get_markers_for_simulation(
            db, roll_plan.cutplan_id, roll_plan.color_code
        )
        if not markers:
            roll_plan.status = RollPlanStatus.failed
            roll_plan.error_message = "No markers found for simulation"
            return

        total_fabric = sum(m.total_fabric_yards for m in markers)
        roll_plan.total_fabric_required = total_fabric

        # Prepare rolls
        rolls = self.prepare_rolls(db, roll_plan, total_fabric)

        pseudo_config = PseudoRollConfig(
            avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
            delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
        )

        mode = roll_plan.mode or RollPlanMode.both
        mc_result: Optional[MonteCarloResult] = None
        ga_result: Optional[GAResult] = None

        # --- Monte Carlo ---
        if mode in (RollPlanMode.monte_carlo, RollPlanMode.both):
            if progress_callback:
                progress_callback(5, "Simulating cutting floor scenarios...")

            mc_result = simulate_roll_usage(
                markers=markers,
                rolls=rolls,  # None for pseudo-only
                pseudo_config=pseudo_config,
                num_simulations=roll_plan.num_simulations or 100,
                min_reuse_length=roll_plan.min_reuse_length_yards or 0.5,
                progress_callback=lambda pct, msg: (
                    progress_callback(5 + int(pct * 0.45), msg) if progress_callback else None
                ),
                cancel_check=cancel_check,
            )

            if cancel_check and cancel_check():
                return

            # Persist MC results — per-category waste stats
            roll_plan.mc_unusable_avg = mc_result.unusable_waste.avg
            roll_plan.mc_unusable_std = mc_result.unusable_waste.std
            roll_plan.mc_unusable_p95 = mc_result.unusable_waste.p95
            roll_plan.mc_endbit_avg = mc_result.endbit_waste.avg
            roll_plan.mc_endbit_std = mc_result.endbit_waste.std
            roll_plan.mc_endbit_p95 = mc_result.endbit_waste.p95
            roll_plan.mc_returnable_avg = mc_result.returnable_waste.avg
            roll_plan.mc_returnable_std = mc_result.returnable_waste.std
            roll_plan.mc_returnable_p95 = mc_result.returnable_waste.p95
            roll_plan.mc_real_waste_avg = mc_result.real_waste.avg
            roll_plan.mc_real_waste_std = mc_result.real_waste.std
            roll_plan.mc_real_waste_p95 = mc_result.real_waste.p95

            # Summary per run (skip full end_bits for storage)
            roll_plan.mc_simulation_runs = [
                {
                    "run_id": r.run_id,
                    "unusable_yards": r.waste.unusable_yards,
                    "endbit_yards": r.waste.endbit_yards,
                    "returnable_yards": r.waste.returnable_yards,
                    "real_waste_yards": r.waste.real_waste_yards,
                    "reused_count": r.reused_count,
                    "rolls_consumed": r.rolls_consumed,
                }
                for r in mc_result.runs
            ]

            # Best run dockets
            roll_plan.mc_best_run_dockets = _serialize_dockets(mc_result.best_run.cut_dockets)

        # --- GA Optimizer ---
        if mode in (RollPlanMode.ga, RollPlanMode.both):
            if progress_callback:
                progress_callback(55, "Optimizing roll-to-marker assignment...")

            # For GA, use a fixed roll set (real + pseudo fill)
            if rolls is None:
                ga_rolls = generate_pseudo_rolls(total_fabric, pseudo_config)
            else:
                ga_rolls = list(rolls)

            ga_result = optimize_rolls_ga(
                markers=markers,
                rolls=ga_rolls,
                min_reuse_length=roll_plan.min_reuse_length_yards or 0.5,
                pop_size=ga_pop_size,
                generations=ga_generations,
                progress_callback=lambda pct, msg: (
                    progress_callback(55 + int(pct * 0.40), msg) if progress_callback else None
                ),
                cancel_check=cancel_check,
            )

            if cancel_check and cancel_check():
                return

            roll_plan.ga_unusable_yards = ga_result.waste.unusable_yards
            roll_plan.ga_endbit_yards = ga_result.waste.endbit_yards
            roll_plan.ga_returnable_yards = ga_result.waste.returnable_yards
            roll_plan.ga_real_waste_yards = ga_result.waste.real_waste_yards
            roll_plan.ga_generations_run = ga_result.generations_run
            roll_plan.ga_dockets = _serialize_dockets(ga_result.cut_dockets)

        roll_plan.progress = 100
        roll_plan.status = RollPlanStatus.completed
        if progress_callback:
            progress_callback(100, "Simulation complete")

    # ------------------------------------------------------------------
    # Build response
    # ------------------------------------------------------------------

    @staticmethod
    def build_response_data(db: Session, roll_plan: RollPlan) -> Dict:
        """Build a dict suitable for RollPlanResponse."""
        rolls_db = (
            db.query(FabricRoll)
            .filter(FabricRoll.roll_plan_id == roll_plan.id)
            .all()
        )
        real_count = sum(1 for r in rolls_db if not r.is_pseudo)
        pseudo_count = sum(1 for r in rolls_db if r.is_pseudo)

        data = {
            "id": roll_plan.id,
            "cutplan_id": roll_plan.cutplan_id,
            "name": roll_plan.name,
            "color_code": roll_plan.color_code,
            "status": roll_plan.status.value if roll_plan.status else "pending",
            "mode": roll_plan.mode.value if roll_plan.mode else "both",
            "input_type": roll_plan.input_type.value if roll_plan.input_type else None,
            "num_simulations": roll_plan.num_simulations or 100,
            "min_reuse_length_yards": roll_plan.min_reuse_length_yards or 0.5,
            "pseudo_roll_avg_yards": roll_plan.pseudo_roll_avg_yards,
            "pseudo_roll_delta_yards": roll_plan.pseudo_roll_delta_yards,
            "progress": roll_plan.progress or 0,
            "progress_message": roll_plan.progress_message,
            "error_message": roll_plan.error_message,
            "rolls_count": len(rolls_db),
            "real_rolls_count": real_count,
            "pseudo_rolls_count": pseudo_count,
            "created_at": roll_plan.created_at,
            "updated_at": roll_plan.updated_at,
        }

        # MC results
        if roll_plan.mc_endbit_avg is not None:
            mc_dockets = _deserialize_dockets(roll_plan.mc_best_run_dockets)
            data["monte_carlo"] = {
                "num_simulations": roll_plan.num_simulations or 100,
                "total_fabric_required": roll_plan.total_fabric_required,
                "unusable_waste": {
                    "avg": roll_plan.mc_unusable_avg or 0,
                    "std": roll_plan.mc_unusable_std or 0,
                    "p95": roll_plan.mc_unusable_p95 or 0,
                },
                "endbit_waste": {
                    "avg": roll_plan.mc_endbit_avg or 0,
                    "std": roll_plan.mc_endbit_std or 0,
                    "p95": roll_plan.mc_endbit_p95 or 0,
                },
                "returnable_waste": {
                    "avg": roll_plan.mc_returnable_avg or 0,
                    "std": roll_plan.mc_returnable_std or 0,
                    "p95": roll_plan.mc_returnable_p95 or 0,
                },
                "real_waste": {
                    "avg": roll_plan.mc_real_waste_avg or 0,
                    "std": roll_plan.mc_real_waste_std or 0,
                    "p95": roll_plan.mc_real_waste_p95 or 0,
                },
                "best_run_dockets": mc_dockets,
            }

        # GA results
        if roll_plan.ga_endbit_yards is not None:
            ga_dockets = _deserialize_dockets(roll_plan.ga_dockets)
            data["ga"] = {
                "waste": {
                    "unusable_yards": roll_plan.ga_unusable_yards or 0,
                    "endbit_yards": roll_plan.ga_endbit_yards or 0,
                    "returnable_yards": roll_plan.ga_returnable_yards or 0,
                    "real_waste_yards": roll_plan.ga_real_waste_yards or 0,
                },
                "generations_run": roll_plan.ga_generations_run,
                "dockets": ga_dockets,
            }

        return data


# ---------------------------------------------------------------------------
# Serialization helpers for CutDocket ↔ JSON
# ---------------------------------------------------------------------------


def _serialize_dockets(dockets) -> List[Dict]:
    """Convert CutDocket list to JSON-serializable dicts."""
    result = []
    for d in dockets:
        result.append({
            "cut_number": d.cut_number,
            "marker_label": d.marker_label,
            "ratio_str": d.ratio_str,
            "marker_length_yards": d.marker_length_yards,
            "plies": d.plies,
            "assigned_rolls": [
                {
                    "roll_id": a.roll_id,
                    "roll_length_yards": a.roll_length_yards,
                    "plies_from_roll": a.plies_from_roll,
                    "end_bit_yards": a.end_bit_yards,
                    "is_pseudo": a.is_pseudo,
                }
                for a in d.assigned_rolls
            ],
            "total_fabric_yards": d.total_fabric_yards,
            "total_end_bit_yards": d.total_end_bit_yards,
        })
    return result


def _deserialize_dockets(json_data) -> List[Dict]:
    """Pass-through: JSON dockets are already in the right format for responses."""
    if not json_data:
        return []
    return json_data
