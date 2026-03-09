import os
import sys
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Cutplan, CutplanMarker, Order, OrderLine, SizeQuantity, MarkerBank, CostConfig, Pattern, PatternFabricMapping

from .ilp_solver_runner import optimize_cutplan, calculate_cutplan_costs


class CutplanService:
    """Service for cutplan optimization using ILP solvers."""

    def create_cutplan(
        self,
        db: Session,
        order_id: str,
        name: str,
        solver_type: str = "single_color"
    ) -> Cutplan:
        """Create a new cutplan."""
        cutplan = Cutplan(
            order_id=order_id,
            name=name,
            solver_type=solver_type,
        )
        db.add(cutplan)
        db.commit()
        db.refresh(cutplan)
        return cutplan

    def get_order_demand(self, db: Session, order_id: str) -> Dict[str, Dict[str, int]]:
        """
        Get demand by color and size for an order.
        Returns: {"NAVY": {"M": 50, "L": 100}, "BLACK": {"M": 30, "L": 80}}
        """
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {}

        demand = {}
        for order_line in order.order_lines:
            color_demand = {}
            for sq in order_line.size_quantities:
                if sq.quantity > 0:
                    color_demand[sq.size_code] = sq.quantity
            if color_demand:
                demand[order_line.color_code] = color_demand

        return demand

    def get_flat_demand(self, db: Session, order_id: str, color_code: Optional[str] = None) -> Dict[str, int]:
        """
        Get total demand per size.
        If color_code is specified, use the first order line for that color
        (all fabric lines for the same color share identical demand).
        Otherwise aggregate across unique colors (one line per color).
        Returns: {"46": 74, "48": 244, ...}
        """
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {}

        flat_demand = {}
        seen_colors = set()
        for order_line in order.order_lines:
            if color_code and order_line.color_code != color_code:
                continue
            # Only count each color once (multiple fabric lines share the same demand)
            if order_line.color_code in seen_colors:
                continue
            seen_colors.add(order_line.color_code)
            for sq in order_line.size_quantities:
                if sq.quantity > 0:
                    flat_demand[sq.size_code] = flat_demand.get(sq.size_code, 0) + sq.quantity

        return flat_demand

    def get_order_sizes(self, db: Session, order_id: str, color_code: Optional[str] = None) -> List[str]:
        """Get list of sizes in the order, in canonical pattern order."""
        demand = self.get_flat_demand(db, order_id, color_code=color_code)
        demand_sizes = set(demand.keys())

        # Try to use the pattern's available_sizes for canonical ordering
        order = db.query(Order).filter(Order.id == order_id).first()
        if order and order.pattern_id:
            pattern = db.query(Pattern).filter(Pattern.id == order.pattern_id).first()
            if pattern and pattern.available_sizes:
                # Return sizes in pattern order, filtered to those in demand
                ordered = [s for s in pattern.available_sizes if s in demand_sizes]
                # Append any demand sizes not in pattern (shouldn't happen, but safe)
                for s in sorted(demand_sizes):
                    if s not in ordered:
                        ordered.append(s)
                return ordered

        return sorted(demand.keys())

    def get_available_markers(
        self,
        db: Session,
        pattern_id: str,
        fabric_id: str
    ) -> List[MarkerBank]:
        """Get all markers from marker bank for a pattern/fabric combo."""
        return db.query(MarkerBank).filter(
            MarkerBank.pattern_id == pattern_id,
            MarkerBank.fabric_id == fabric_id
        ).all()

    def get_cost_config(self, db: Session, customer_id: str) -> CostConfig:
        """Get cost configuration for customer."""
        config = db.query(CostConfig).filter(
            CostConfig.customer_id == customer_id
        ).first()

        if not config:
            # Create default config
            config = CostConfig(customer_id=customer_id)
            db.add(config)
            db.commit()
            db.refresh(config)

        return config

    def calculate_costs(
        self,
        cutplan: Cutplan,
        cost_config: CostConfig,
        markers: List[Dict]
    ) -> Dict[str, float]:
        """
        Calculate cost breakdown for a cutplan.
        Uses ilp_solver_runner.calculate_cutplan_costs internally.
        """
        total_yards = 0.0
        total_plies = 0
        total_cuts = 0
        unique_markers = len(markers)

        for marker in markers:
            plies = marker.get("total_plies", 0)
            length_yards = marker.get("length_yards", 0)
            cuts = (plies + cost_config.max_ply_height - 1) // cost_config.max_ply_height

            total_yards += length_yards * plies
            total_plies += plies
            total_cuts += cuts

        # Calculate costs
        fabric_cost = total_yards * cost_config.fabric_cost_per_yard
        spreading_cost = total_yards * cost_config.spreading_cost_per_yard

        # Cutting cost based on marker length (perimeter)
        avg_marker_perimeter_inches = 100
        cutting_cost = total_cuts * avg_marker_perimeter_inches * cost_config.cutting_cost_per_inch

        # Prep cost per unique marker
        prep_cost = unique_markers * cost_config.prep_cost_per_marker

        total_cost = fabric_cost + spreading_cost + cutting_cost + prep_cost

        return {
            "total_cost": total_cost,
            "fabric_cost": fabric_cost,
            "spreading_cost": spreading_cost,
            "cutting_cost": cutting_cost,
            "prep_cost": prep_cost,
            "total_yards": total_yards,
            "total_plies": total_plies,
            "total_cuts": total_cuts,
            "unique_markers": unique_markers,
        }

    def run_ilp_optimization(
        self,
        db: Session,
        cutplan: Cutplan,
        demand: Dict[str, Dict[str, int]],
        markers: List[MarkerBank],
        solver_config: Dict[str, Any]
    ) -> List[Dict]:
        """
        Run ILP optimization to select markers.
        Wraps the logic from ilp_solver_runner.py
        """
        cutplan.status = "optimizing"
        db.commit()

        try:
            # Get flat demand (aggregate across colors for single-color solver)
            order = db.query(Order).filter(Order.id == cutplan.order_id).first()
            flat_demand = self.get_flat_demand(db, cutplan.order_id)

            if not flat_demand:
                raise ValueError("No demand data available")

            # Get sizes from order
            sizes = self.get_order_sizes(db, cutplan.order_id)

            # Convert MarkerBank objects to dicts
            marker_dicts = [
                {
                    "ratio_str": m.ratio_str,
                    "efficiency": m.efficiency,
                    "length_yards": m.length_yards,
                    "bundle_count": sum(int(x) for x in m.ratio_str.split("-")),
                    "perimeter_cm": (m.extra_data or {}).get("perimeter_cm"),
                }
                for m in markers
            ]

            # Get pattern's canonical sizes for ratio_str parsing
            pattern_sizes = None
            try:
                pattern = db.query(Pattern).filter(Pattern.id == order.pattern_id).first()
                if pattern and pattern.available_sizes:
                    pattern_sizes = list(pattern.available_sizes)
            except Exception:
                pass

            # Determine which strategies to run based on cutplan name
            options = solver_config.get("options", ["max_efficiency", "balanced", "min_markers"])

            # Get penalty from config
            penalty = solver_config.get("penalty", 5.0)

            # Run optimization
            cutplan_options = optimize_cutplan(
                demand=flat_demand,
                markers=marker_dicts,
                sizes=sizes,
                options=options,
                penalty=penalty,
                pattern_sizes=pattern_sizes,
            )

            if not cutplan_options:
                raise ValueError("No cutplan solutions found")

            # Use the first option (usually max_efficiency or balanced)
            selected_option = cutplan_options[0]
            selected_markers = selected_option.get("markers", [])

            # Mark complete
            cutplan.status = "ready"
            cutplan.efficiency = selected_option.get("efficiency", 0)
            db.commit()

            return selected_markers

        except Exception as e:
            cutplan.status = "draft"
            db.commit()
            raise

    def run_multi_strategy_optimization(
        self,
        db: Session,
        order_id: str,
        pattern_id: str,
        fabric_id: str,
        customer_id: str,
        strategies: List[str] = None,
        penalty: float = 5.0,
        progress_callback: Any = None,
        cancel_check: Any = None,
        color_code: Optional[str] = None,
        fabric_cost_per_yard: Optional[float] = None,
        max_ply_height: Optional[int] = None,
        min_plies_by_bundle: Optional[str] = None,
        avg_roll_length_yards: Optional[float] = None,
    ) -> List[Cutplan]:
        """
        Run ILP optimization with multiple strategies and create cutplans for each.
        Each strategy result is saved immediately when complete.

        Args:
            db: Database session
            order_id: Order ID
            pattern_id: Pattern ID
            fabric_id: Fabric ID
            customer_id: Customer ID for cost config
            strategies: List of strategies to run
            penalty: Penalty for balanced strategy
            progress_callback: Optional (progress_pct, message) callback
            cancel_check: Optional callable returning True to cancel
            color_code: Optional color to filter demand (None = all colors)

        Returns:
            List of Cutplan objects
        """
        if strategies is None:
            strategies = ["max_efficiency", "balanced", "min_markers"]

        # Get demand (filtered by color if specified)
        flat_demand = self.get_flat_demand(db, order_id, color_code=color_code)
        if not flat_demand:
            color_msg = f" for color '{color_code}'" if color_code else ""
            raise ValueError(f"Order has no quantities{color_msg}")

        sizes = self.get_order_sizes(db, order_id, color_code=color_code)

        # Get markers
        markers = self.get_available_markers(db, pattern_id, fabric_id)
        if not markers:
            raise ValueError("No markers available. Run nesting first.")

        marker_dicts = [
            {
                "ratio_str": m.ratio_str,
                "efficiency": m.efficiency,
                "length_yards": m.length_yards,
                "bundle_count": sum(int(x) for x in m.ratio_str.split("-")),
                "perimeter_cm": (m.extra_data or {}).get("perimeter_cm"),
            }
            for m in markers
        ]

        # Get pattern's canonical sizes for ratio_str parsing
        # (pattern may have more sizes than the order demands)
        pattern_sizes = None
        try:
            pattern_obj = db.query(Pattern).filter(Pattern.id == pattern_id).first()
            if pattern_obj and pattern_obj.available_sizes:
                pattern_sizes = list(pattern_obj.available_sizes)
                if len(pattern_sizes) != len(sizes):
                    print(f"[CutplanService] Pattern has {len(pattern_sizes)} sizes {pattern_sizes}, "
                          f"order demands {len(sizes)} sizes {sizes} — will remap ratio strings")
        except Exception as e:
            print(f"[CutplanService] Warning: could not load pattern sizes: {e}")

        color_label = f" [{color_code}]" if color_code else ""
        total_garments = sum(flat_demand.values())

        if progress_callback:
            progress_callback(5, f"Loaded {len(marker_dicts)} markers{color_label}, {total_garments} garments across {len(sizes)} sizes...")

        # Get cost config, override fabric cost, max ply height, min plies if user specified
        cost_config = self.get_cost_config(db, customer_id)
        effective_fabric_cost = fabric_cost_per_yard if fabric_cost_per_yard is not None else cost_config.fabric_cost_per_yard
        effective_max_ply_height = max_ply_height if max_ply_height is not None else cost_config.max_ply_height
        effective_min_plies_str = min_plies_by_bundle if min_plies_by_bundle is not None else cost_config.min_plies_by_bundle

        # Load perimeter_by_size from pattern parse_metadata
        perimeter_for_material = None
        try:
            pattern = db.query(Pattern).filter(Pattern.id == pattern_id).first()
            if pattern and pattern.parse_metadata:
                perim_data = pattern.parse_metadata.get("perimeter_by_size", {})
                if perim_data:
                    # Find which material maps to this fabric_id
                    mapping = db.query(PatternFabricMapping).filter(
                        PatternFabricMapping.pattern_id == pattern_id,
                        PatternFabricMapping.fabric_id == fabric_id,
                    ).first()
                    if mapping and mapping.material_name in perim_data:
                        perimeter_for_material = perim_data[mapping.material_name]
                    elif len(perim_data) == 1:
                        # Single material — use it directly
                        perimeter_for_material = list(perim_data.values())[0]
        except Exception as e:
            print(f"[CutplanService] Warning: could not load perimeter_by_size: {e}")

        # Compute cutting cost per cm from input params
        cutting_cost_per_cm = (
            (cost_config.cutting_labor_cost_per_hour * cost_config.cutting_workers_per_cut)
            / 3600.0
        ) / cost_config.cutting_speed_cm_per_s

        # Compute prep cost per meter from enabled paper layers
        prep_cost_per_m = 0.0
        if getattr(cost_config, 'prep_perf_paper_enabled', True):
            prep_cost_per_m += getattr(cost_config, 'prep_perf_paper_cost_per_m', 0.1)
        if getattr(cost_config, 'prep_underlayer_enabled', True):
            prep_cost_per_m += getattr(cost_config, 'prep_underlayer_cost_per_m', 0.1)
        if getattr(cost_config, 'prep_top_layer_enabled', True):
            prep_cost_per_m += getattr(cost_config, 'prep_top_layer_cost_per_m', 0.05)

        # Build marker_bank lookup: ratio_str -> MarkerBank.id
        # Include both original ratio_str AND trimmed (order-sizes-only) version
        marker_bank_lookup = {m.ratio_str: m.id for m in markers}
        if pattern_sizes and len(pattern_sizes) != len(sizes):
            order_sizes_set = set(sizes)
            for m in markers:
                parts = m.ratio_str.split("-")
                if len(parts) == len(pattern_sizes):
                    full_ratio = {s: int(parts[i]) for i, s in enumerate(pattern_sizes)}
                    trimmed = "-".join(str(full_ratio.get(s, 0)) for s in sizes)
                    if trimmed not in marker_bank_lookup:
                        marker_bank_lookup[trimmed] = m.id

        # Track cutplans created incrementally
        cutplans = []
        completed_strategies = 0

        def on_strategy_complete(strategy_name: str, option: Dict):
            """Called when each strategy finishes — save result immediately."""
            nonlocal completed_strategies

            cutplan = self.create_cutplan(
                db=db,
                order_id=order_id,
                name=option.get("name", "Cutplan"),
                solver_type="single_color",
            )
            # Persist solver params so exports can use them later
            cutplan.solver_config = {
                "max_ply_height": effective_max_ply_height,
                "penalty": penalty,
                "fabric_cost_per_yard": effective_fabric_cost,
            }

            # Save markers with stable labels and MarkerBank links
            selected_markers = option.get("markers", [])
            self.save_cutplan_markers(db, cutplan, selected_markers, marker_bank_lookup=marker_bank_lookup)

            # Calculate and save costs
            costs = calculate_cutplan_costs(
                option,
                fabric_cost_per_yard=effective_fabric_cost,
                max_ply_height=effective_max_ply_height,
                spreading_cost_per_yard=cost_config.spreading_cost_per_yard,
                spreading_cost_per_ply=getattr(cost_config, 'spreading_cost_per_ply', 0.013),
                cutting_cost_per_cm=cutting_cost_per_cm,
                prep_cost_per_meter=prep_cost_per_m,
                perimeter_by_size=perimeter_for_material,
                sizes=sizes,
            )

            # Update cutplan with summary
            cutplan.status = "ready"
            cutplan.efficiency = option.get("efficiency", 0)
            cutplan.unique_markers = costs["unique_markers"]
            cutplan.total_cuts = costs["total_cuts"]
            cutplan.total_plies = costs["total_plies"]
            cutplan.total_yards = costs["total_yards"]
            cutplan.total_cost = costs["total_cost"]
            cutplan.fabric_cost = costs["fabric_cost"]
            cutplan.spreading_cost = costs["spreading_cost"]
            cutplan.cutting_cost = costs["cutting_cost"]
            cutplan.prep_cost = costs["prep_cost"]
            cutplan.bundle_cuts = option.get("bundle_cuts", 0)
            db.commit()

            cutplans.append(cutplan)
            completed_strategies += 1

            solve_time = option.get("solve_time", 0)
            if progress_callback:
                pct = int(10 + (completed_strategies / len(strategies)) * 85)
                progress_callback(pct, f"Strategy {completed_strategies}/{len(strategies)} done: "
                                      f"{option.get('name', strategy_name)} — "
                                      f"{option.get('efficiency', 0)*100:.1f}% eff "
                                      f"({solve_time:.0f}s)")

        if progress_callback:
            progress_callback(10, f"Running ILP solver: {len(strategies)} strategies, penalty={penalty}...")

        # Run optimization with incremental callback
        optimize_cutplan(
            demand=flat_demand,
            markers=marker_dicts,
            sizes=sizes,
            options=strategies,
            penalty=penalty,
            strategy_callback=on_strategy_complete,
            cancel_check=cancel_check,
            pattern_sizes=pattern_sizes,
            max_ply_height=effective_max_ply_height,
            min_plies_by_bundle_str=effective_min_plies_str,
            avg_roll_length_yards=avg_roll_length_yards,
        )

        # Update order status if any strategies completed
        if cutplans:
            order = db.query(Order).filter(Order.id == order_id).first()
            if order and order.status in ("pending_cutplan",):
                order.status = "cutplan_ready"
                db.commit()

        if progress_callback:
            progress_callback(100, f"Complete — {len(cutplans)} cutplan options generated")

        return cutplans

    def run_multicolor_optimization(
        self,
        db: Session,
        cutplan: Cutplan,
        demand: Dict[str, Dict[str, int]],
        markers_by_color: Dict[str, List[MarkerBank]],
        solver_config: Dict[str, Any]
    ) -> List[Dict]:
        """
        Run multicolor joint ILP optimization.
        This wraps the logic from scripts/multicolor_solver.py
        """
        # TODO: Implement multicolor optimization
        return []

    def run_two_stage_optimization(
        self,
        db: Session,
        cutplan: Cutplan,
        demand: Dict[str, Dict[str, int]],
        markers: List[MarkerBank],
        solver_config: Dict[str, Any]
    ) -> List[Dict]:
        """
        Run two-stage ILP optimization.
        This wraps the logic from scripts/multicolor_solver_twostage.py
        """
        # TODO: Implement two-stage optimization
        return []

    def save_cutplan_markers(
        self,
        db: Session,
        cutplan: Cutplan,
        selected_markers: List[Dict],
        marker_bank_lookup: Optional[Dict[str, str]] = None,
    ):
        """Save selected markers to cutplan with stable labels.

        Args:
            marker_bank_lookup: Optional dict of ratio_str -> MarkerBank.id
                                for linking cutplan markers to their GPU source.
        """
        for idx, marker_data in enumerate(selected_markers):
            label = f"M{idx + 1}"
            marker_id = marker_data.get("marker_id")
            if not marker_id and marker_bank_lookup:
                marker_id = marker_bank_lookup.get(marker_data["ratio_str"])

            cm = CutplanMarker(
                cutplan_id=cutplan.id,
                marker_id=marker_id,
                marker_label=label,
                ratio_str=marker_data["ratio_str"],
                efficiency=marker_data.get("efficiency"),
                length_yards=marker_data.get("length_yards"),
                plies_by_color=marker_data.get("plies_by_color", {}),
                total_plies=marker_data.get("total_plies", 0),
                cuts=marker_data.get("cuts", 0),
            )
            db.add(cm)

        db.commit()

    def approve_cutplan(self, db: Session, cutplan_id: str) -> Cutplan:
        """Approve a cutplan for production."""
        cutplan = db.query(Cutplan).filter(Cutplan.id == cutplan_id).first()
        if cutplan:
            cutplan.status = "approved"
            db.commit()
            db.refresh(cutplan)

            # Update order status
            order = db.query(Order).filter(Order.id == cutplan.order_id).first()
            if order:
                order.status = "approved"
                db.commit()

        return cutplan


# Singleton instance
cutplan_service = CutplanService()
