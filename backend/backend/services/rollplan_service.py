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

import statistics
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Cutplan, Order
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
    PreflightResult,
    PseudoRollConfig,
    RollSpec,
    WasteStats,
    generate_pseudo_rolls,
    optimize_rolls_ga,
    parse_roll_excel,
    simulate_roll_usage,
    validate_rollplan_inputs,
)
from .endbit_solver import estimate_floor_waste, floor_mc_with_rolls


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
        confirm_shortfall: bool = False,
    ) -> Tuple[Optional[List[RollSpec]], Optional[str]]:
        """
        Load real rolls from DB, trim excess, or detect shortfall.

        Returns (rolls, adjustment_message).
          - rolls=None with no real rolls → pseudo-only mode
          - rolls=None with real rolls + shortfall not confirmed → "needs_confirmation"

        Trimming (excess): automatic, no confirmation needed.
        Padding (shortfall): requires confirm_shortfall=True from user.
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

        if not real_rolls:
            roll_plan.input_type = RollInputType.pseudo
            return None, None  # Signal to use generated rolls (regenerated per MC run)

        threshold = roll_plan.waste_threshold_pct if roll_plan.waste_threshold_pct is not None else 2.0
        target = total_fabric_needed * (1.0 + threshold / 100.0)
        real_total = sum(r.length_yards for r in real_rolls)

        if real_total >= target:
            # Excess: trim shortest rolls until just above target (auto, no confirmation)
            adjusted = sorted(real_rolls, key=lambda r: r.length_yards, reverse=True)
            removed = 0
            running = real_total
            while len(adjusted) > 1:
                if running - adjusted[-1].length_yards >= target:
                    running -= adjusted[-1].length_yards
                    adjusted.pop()
                    removed += 1
                else:
                    break
            roll_plan.input_type = RollInputType.real
            msg = f"{removed} excess rolls removed to match cutplan+{threshold:.0f}% requirement" if removed else None
            return adjusted, msg

        else:
            # Shortfall: need user confirmation before adding generated rolls
            shortfall = target - real_total
            config = PseudoRollConfig(
                avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
                delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
            )
            median_len = statistics.median([r.length_yards for r in real_rolls])
            rolls_needed = max(1, int(shortfall / median_len) + 1)

            if not confirm_shortfall:
                # Block simulation — return shortfall info for frontend
                roll_plan.roll_adjustment_message = (
                    f"Shortfall: uploaded {real_total:.1f} yd, need {target:.1f} yd "
                    f"(cutplan {total_fabric_needed:.1f} yd + {threshold:.0f}% buffer). "
                    f"Approve adding ~{rolls_needed} generated roll(s) "
                    f"(~{median_len:.0f} yd each) to proceed."
                )
                roll_plan.input_type = RollInputType.real
                return real_rolls, "needs_confirmation"

            # User confirmed: pad with generated rolls
            pseudo = generate_pseudo_rolls(total_fabric_needed, config, real_rolls)
            roll_plan.input_type = RollInputType.mixed
            msg = f"{len(pseudo)} generated rolls added to cover {shortfall:.1f} yd shortfall"
            return real_rolls + pseudo, msg

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
        confirm_shortfall: bool = False,
    ) -> Optional[str]:
        """
        Orchestrate MC + GA simulation, save results to roll_plan.

        Updates roll_plan in place. Caller should commit after.

        Returns:
            None on success (or cancel/fail).
            "needs_confirmation" if real rolls have a shortfall and user
            hasn't confirmed adding generated rolls yet.
        """
        markers = self.get_markers_for_simulation(
            db, roll_plan.cutplan_id, roll_plan.color_code
        )
        if not markers:
            roll_plan.status = RollPlanStatus.failed
            roll_plan.error_message = "No markers found for simulation"
            return None

        total_fabric = sum(m.total_fabric_yards for m in markers)
        roll_plan.total_fabric_required = total_fabric

        # Auto-derive min_reuse_length from cutplan markers
        total_garments = sum(
            sum(int(x) for x in m.ratio_str.split("-")) * m.plies
            for m in markers
        )
        if total_garments > 0:
            roll_plan.min_reuse_length_yards = round(total_fabric / total_garments, 4)

        # Read max_ply_height from cutplan solver_config (default 100)
        from ..models.cutplan import Cutplan
        cutplan = db.query(Cutplan).filter(Cutplan.id == roll_plan.cutplan_id).first()
        max_ply_height = 100
        if cutplan and cutplan.solver_config and isinstance(cutplan.solver_config, dict):
            max_ply_height = cutplan.solver_config.get("max_ply_height", 100)

        # Prepare rolls (may block on shortfall confirmation)
        rolls, adjustment_message = self.prepare_rolls(
            db, roll_plan, total_fabric, confirm_shortfall=confirm_shortfall
        )

        if adjustment_message == "needs_confirmation":
            # Shortfall detected, user hasn't confirmed yet — halt simulation
            roll_plan.status = RollPlanStatus.pending
            return "needs_confirmation"

        roll_plan.roll_adjustment_message = adjustment_message

        # Pre-flight validation
        pseudo_config = PseudoRollConfig(
            avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
            delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
        )
        preflight = validate_rollplan_inputs(markers, rolls, pseudo_config)
        if preflight.warnings:
            roll_plan.preflight_warnings = [
                {"level": w.level, "message": w.message}
                for w in preflight.warnings
            ]
        else:
            roll_plan.preflight_warnings = None

        mode = roll_plan.mode or RollPlanMode.both
        mc_result: Optional[MonteCarloResult] = None
        ga_result: Optional[GAResult] = None

        # --- Monte Carlo (floor MC v2: endbit_priority=True for evaluation) ---
        if mode in (RollPlanMode.monte_carlo, RollPlanMode.both):
            if progress_callback:
                progress_callback(5, "Simulating cutting floor scenarios...")

            n_sims = roll_plan.num_simulations or 100
            threshold_raw = roll_plan.waste_threshold_pct if roll_plan.waste_threshold_pct is not None else 2.0
            buffer_frac = max(threshold_raw / 100.0, 0.01)  # minimum 1%

            if rolls is not None:
                # Real/mixed rolls — run MC directly on prepared rolls (no double-padding)
                floor_result = floor_mc_with_rolls(
                    mc_specs=markers,
                    rolls=rolls,
                    n_sims=n_sims,
                    max_ply_height=max_ply_height,
                    endbit_priority=True,
                )

                # If completion < 100% with real rolls, try adding more
                completion = floor_result.get("completion_rate", 0)
                if completion < 1.0 and rolls:
                    # Add generated rolls to cover the gap and re-run
                    config = PseudoRollConfig(
                        avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
                        delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
                    )
                    extra = generate_pseudo_rolls(total_fabric * 0.05, config)  # add ~5% more
                    augmented = list(rolls) + extra
                    floor_result = floor_mc_with_rolls(
                        mc_specs=markers,
                        rolls=augmented,
                        n_sims=n_sims,
                        max_ply_height=max_ply_height,
                        endbit_priority=True,
                    )
                    if not roll_plan.roll_adjustment_message:
                        roll_plan.roll_adjustment_message = (
                            f"{len(extra)} additional generated rolls added during simulation "
                            f"to achieve full order completion"
                        )
            else:
                # Generated-only — use estimate_floor_waste with auto-escalation
                avg_roll = roll_plan.pseudo_roll_avg_yards or 100.0
                waste_est = estimate_floor_waste(
                    mc_specs=markers,
                    max_ply_height=max_ply_height,
                    avg_roll_length=avg_roll,
                    n_sims=n_sims,
                    start_buffer_pct=buffer_frac,
                    max_buffer_pct=0.15,
                    buffer_step_pct=0.01,
                    endbit_priority=True,
                )
                floor_result = waste_est.get("_mc_result", {})

            if cancel_check and cancel_check():
                return

            # Map _floor_mc result dict → rollplan model fields
            runs_data = floor_result.get("runs", [])
            if runs_data:
                import math as _math

                t1_vals = [r["type1"] for r in runs_data]
                t2_vals = [r["type2"] for r in runs_data]
                t3_vals = [r["type3"] for r in runs_data]
                real_vals = [r["type1"] + r["type2"] for r in runs_data]

                def _stats(vals):
                    """Build WasteStats-compatible values from a list."""
                    sv = sorted(vals)
                    n = len(sv)
                    p95_idx = max(0, int(_math.ceil(0.95 * n)) - 1)
                    return {
                        "avg": round(statistics.mean(vals), 4),
                        "std": round(statistics.stdev(vals) if n > 1 else 0, 4),
                        "p95": round(sv[p95_idx], 4),
                    }

                s1 = _stats(t1_vals)
                s2 = _stats(t2_vals)
                s3 = _stats(t3_vals)
                sr = _stats(real_vals)

                roll_plan.mc_unusable_avg = s1["avg"]
                roll_plan.mc_unusable_std = s1["std"]
                roll_plan.mc_unusable_p95 = s1["p95"]
                roll_plan.mc_endbit_avg = s2["avg"]
                roll_plan.mc_endbit_std = s2["std"]
                roll_plan.mc_endbit_p95 = s2["p95"]
                roll_plan.mc_returnable_avg = s3["avg"]
                roll_plan.mc_returnable_std = s3["std"]
                roll_plan.mc_returnable_p95 = s3["p95"]
                roll_plan.mc_real_waste_avg = sr["avg"]
                roll_plan.mc_real_waste_std = sr["std"]
                roll_plan.mc_real_waste_p95 = sr["p95"]

                # Per-run summary (floor MC has no dockets/reuse tracking)
                roll_plan.mc_simulation_runs = [
                    {
                        "run_id": i,
                        "unusable_yards": r["type1"],
                        "endbit_yards": r["type2"],
                        "returnable_yards": r["type3"],
                        "real_waste_yards": r["type1"] + r["type2"],
                        "reused_count": 0,
                        "rolls_consumed": 0,
                    }
                    for i, r in enumerate(runs_data)
                ]

                # No dockets from floor MC
                roll_plan.mc_best_run_dockets = []

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
                max_ply_height=max_ply_height,
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
    # Tune cutplan (re-run ILP with roll_optimized strategy)
    # ------------------------------------------------------------------

    def tune_cutplan(
        self,
        db: Session,
        roll_plan: RollPlan,
        avg_roll_length_yards: float,
        roll_penalty_weight: float = 2.0,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """
        Re-run the same strategy as the original cutplan but with roll-length
        awareness.  Creates a NEW cutplan.

        Returns the new cutplan ID.
        """
        from .cutplan_service import CutplanService

        cutplan = db.query(Cutplan).filter(Cutplan.id == roll_plan.cutplan_id).first()
        if not cutplan:
            raise ValueError("Original cutplan not found")

        order = db.query(Order).filter(Order.id == cutplan.order_id).first()
        if not order:
            raise ValueError("Order not found")

        if not order.pattern_id:
            raise ValueError("Order has no pattern assigned")

        # Find the fabric_id from the cutplan's markers
        cutplan_marker = (
            db.query(CutplanMarker)
            .filter(CutplanMarker.cutplan_id == cutplan.id)
            .first()
        )
        if not cutplan_marker or not cutplan_marker.marker_id:
            raise ValueError("No markers found in cutplan")

        from ..models import MarkerBank
        marker_bank = db.query(MarkerBank).filter(MarkerBank.id == cutplan_marker.marker_id).first()
        if not marker_bank:
            raise ValueError("Marker bank entry not found")

        fabric_id = marker_bank.fabric_id

        # Recover user-specified params from original cutplan's solver_config
        orig_cfg = cutplan.solver_config or {}
        fabric_cost = orig_cfg.get("fabric_cost_per_yard")
        max_ply = orig_cfg.get("max_ply_height")
        min_plies = orig_cfg.get("min_plies_by_bundle")

        # Tune always runs EndBit optimized — the user clicks Tune to minimize
        # end-bit waste, regardless of the original cutplan strategy (A/B/C/D).
        strategy = ["endbit_optimized"]

        # Reconstruct the rolls used in evaluation so the endbit solver
        # sees the actual fabric inventory (real, pseudo, or mixed).
        eval_markers = self.get_markers_for_simulation(
            db, roll_plan.cutplan_id, roll_plan.color_code
        )
        eval_fabric = sum(m.total_fabric_yards for m in eval_markers) if eval_markers else 0
        threshold = roll_plan.waste_threshold_pct if roll_plan.waste_threshold_pct is not None else 2.0

        # Load real rolls from DB
        real_rolls_db = (
            db.query(FabricRoll)
            .filter(
                FabricRoll.roll_plan_id == roll_plan.id,
                FabricRoll.is_pseudo == False,
            )
            .all()
        )
        real_rolls_list = [
            RollSpec(
                roll_id=r.roll_number,
                length_yards=r.length_yards,
                is_pseudo=False,
                width_inches=r.width_inches,
            )
            for r in real_rolls_db
        ]

        if real_rolls_list:
            # Real rolls exist — pad with pseudo if the evaluation did so
            real_total = sum(r.length_yards for r in real_rolls_list)
            target = eval_fabric * (1.0 + threshold / 100.0)
            if real_total >= target:
                rolls_for_tune = real_rolls_list
            else:
                pseudo_cfg = PseudoRollConfig(
                    avg_length_yards=roll_plan.pseudo_roll_avg_yards or 100.0,
                    delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
                )
                pseudo_pad = generate_pseudo_rolls(eval_fabric, pseudo_cfg, real_rolls_list)
                rolls_for_tune = real_rolls_list + pseudo_pad
                print(f"[RollPlanService] Tune: {len(real_rolls_list)} real + "
                      f"{len(pseudo_pad)} pseudo rolls")
        else:
            # Pseudo-only plan — generate rolls matching evaluation config
            pseudo_cfg = PseudoRollConfig(
                avg_length_yards=roll_plan.pseudo_roll_avg_yards or avg_roll_length_yards,
                delta_yards=roll_plan.pseudo_roll_delta_yards or 20.0,
            )
            rolls_for_tune = generate_pseudo_rolls(eval_fabric, pseudo_cfg)
            print(f"[RollPlanService] Tune: generated {len(rolls_for_tune)} pseudo rolls "
                  f"(avg {pseudo_cfg.avg_length_yards:.0f}yd)")

        print(f"[RollPlanService] Tune: endbit_optimized with {len(rolls_for_tune)} rolls")

        svc = CutplanService()
        results = svc.run_multi_strategy_optimization(
            db=db,
            order_id=cutplan.order_id,
            pattern_id=order.pattern_id,
            fabric_id=fabric_id,
            customer_id=order.customer_id,
            strategies=strategy,
            penalty=roll_penalty_weight,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            avg_roll_length_yards=avg_roll_length_yards,
            fabric_width_inches=marker_bank.fabric_width_inches,
            fabric_cost_per_yard=fabric_cost,
            max_ply_height=max_ply,
            min_plies_by_bundle=min_plies if isinstance(min_plies, str) else None,
            cost_metric="length",
            real_rolls=rolls_for_tune,
            endbit_pad_pct=0.05 if rolls_for_tune else 0.0,
        )

        if not results:
            raise ValueError("ILP solver produced no results")

        new_cutplan = results[0]
        new_cutplan.name = "Roll-Tuned (EndBit)"
        db.commit()

        return new_cutplan.id

    # ------------------------------------------------------------------
    # Build response
    # ------------------------------------------------------------------

    @staticmethod
    def compute_waste_assessment(
        roll_plan: RollPlan,
        threshold_pct: float = 1.0,
    ) -> Optional[Dict]:
        """
        Compute waste assessment from MC results.

        Returns dict with waste_pct, exceeds_threshold, threshold_pct,
        waste_yards, total_fabric_yards, and recommendation.
        """
        if roll_plan.mc_unusable_avg is None or roll_plan.mc_endbit_avg is None:
            return None

        total_fabric = roll_plan.total_fabric_required or 0
        if total_fabric <= 0:
            return None

        avg_unusable = roll_plan.mc_unusable_avg or 0
        avg_endbit = roll_plan.mc_endbit_avg or 0
        waste_yards = avg_unusable + avg_endbit
        waste_pct = waste_yards / total_fabric * 100

        exceeds = waste_pct > threshold_pct
        if exceeds:
            recommendation = (
                f"{waste_yards:.1f} yards wasted of {total_fabric:.0f} yards needed ({waste_pct:.2f}%, threshold: {threshold_pct}%). "
                f"Consider optimizing the roll plan to reduce end-bit waste."
            )
        else:
            recommendation = (
                f"{waste_yards:.1f} yards wasted of {total_fabric:.0f} yards needed ({waste_pct:.2f}%) — within acceptable levels."
            )

        return {
            "waste_pct": round(waste_pct, 2),
            "exceeds_threshold": exceeds,
            "threshold_pct": threshold_pct,
            "waste_yards": round(waste_yards, 2),
            "total_fabric_yards": round(total_fabric, 1),
            "recommendation": recommendation,
        }

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
            "preflight_warnings": roll_plan.preflight_warnings,
            "roll_adjustment_message": roll_plan.roll_adjustment_message,
            "created_at": roll_plan.created_at,
            "updated_at": roll_plan.updated_at,
        }

        # Waste assessment
        threshold = roll_plan.waste_threshold_pct if roll_plan.waste_threshold_pct is not None else 2.0
        waste_assessment = RollPlanService.compute_waste_assessment(roll_plan, threshold_pct=threshold)
        if waste_assessment:
            data["waste_assessment"] = waste_assessment

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
            "plies_planned": d.plies_planned,
            "assigned_rolls": [
                {
                    "roll_id": a.roll_id,
                    "roll_length_yards": a.roll_length_yards,
                    "plies_from_roll": a.plies_from_roll,
                    "end_bit_yards": a.end_bit_yards,
                    "is_pseudo": a.is_pseudo,
                    "fabric_used_yards": a.fabric_used_yards,
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
