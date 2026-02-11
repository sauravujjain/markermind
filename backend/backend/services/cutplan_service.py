import os
import sys
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Cutplan, CutplanMarker, Order, OrderLine, SizeQuantity, MarkerBank, CostConfig

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

    def get_flat_demand(self, db: Session, order_id: str) -> Dict[str, int]:
        """
        Get total demand per size (aggregated across all colors).
        Returns: {"46": 74, "48": 244, ...}
        """
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {}

        flat_demand = {}
        for order_line in order.order_lines:
            for sq in order_line.size_quantities:
                if sq.quantity > 0:
                    flat_demand[sq.size_code] = flat_demand.get(sq.size_code, 0) + sq.quantity

        return flat_demand

    def get_order_sizes(self, db: Session, order_id: str) -> List[str]:
        """Get list of sizes in the order, sorted."""
        demand = self.get_flat_demand(db, order_id)
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
                }
                for m in markers
            ]

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
    ) -> List[Cutplan]:
        """
        Run ILP optimization with multiple strategies and create cutplans for each.

        Args:
            db: Database session
            order_id: Order ID
            pattern_id: Pattern ID
            fabric_id: Fabric ID
            customer_id: Customer ID for cost config
            strategies: List of strategies to run
            penalty: Penalty for balanced strategy

        Returns:
            List of Cutplan objects
        """
        if strategies is None:
            strategies = ["max_efficiency", "balanced", "min_markers"]

        # Get demand
        flat_demand = self.get_flat_demand(db, order_id)
        if not flat_demand:
            raise ValueError("Order has no quantities")

        sizes = self.get_order_sizes(db, order_id)

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
            }
            for m in markers
        ]

        # Get cost config
        cost_config = self.get_cost_config(db, customer_id)

        # Run optimization
        cutplan_options = optimize_cutplan(
            demand=flat_demand,
            markers=marker_dicts,
            sizes=sizes,
            options=strategies,
            penalty=penalty,
        )

        # Create cutplan for each result
        cutplans = []
        for option in cutplan_options:
            cutplan = self.create_cutplan(
                db=db,
                order_id=order_id,
                name=option.get("name", "Cutplan"),
                solver_type="single_color",
            )

            # Save markers
            selected_markers = option.get("markers", [])
            self.save_cutplan_markers(db, cutplan, selected_markers)

            # Calculate and save costs
            costs = calculate_cutplan_costs(
                option,
                fabric_cost_per_yard=cost_config.fabric_cost_per_yard,
                max_ply_height=cost_config.max_ply_height,
                spreading_cost_per_yard=cost_config.spreading_cost_per_yard,
                cutting_cost_per_inch=cost_config.cutting_cost_per_inch,
                prep_cost_per_marker=cost_config.prep_cost_per_marker,
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
            db.commit()

            cutplans.append(cutplan)

        # Update order status
        order = db.query(Order).filter(Order.id == order_id).first()
        if order and order.status in ("pending_cutplan",):
            order.status = "cutplan_ready"
            db.commit()

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
        selected_markers: List[Dict]
    ):
        """Save selected markers to cutplan."""
        for marker_data in selected_markers:
            cm = CutplanMarker(
                cutplan_id=cutplan.id,
                marker_id=marker_data.get("marker_id"),
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
