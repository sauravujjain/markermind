import os
import sys
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Cutplan, CutplanMarker, Order, OrderColor, SizeQuantity, MarkerBank, CostConfig

# Add scripts to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))


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
        Based on scripts/cutplan_cost_analysis_v2.py methodology.
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
        # Simplified: assume average marker is 100 inches perimeter
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
        This wraps the logic from scripts/marker_selection_optimizer_v2.py
        """
        cutplan.status = "optimizing"
        db.commit()

        try:
            # TODO: Import and call actual ILP solver
            # from scripts.marker_selection_optimizer_v2 import optimize_marker_selection

            # For now, placeholder implementation
            selected_markers = []

            # Mark complete
            cutplan.status = "ready"
            db.commit()

            return selected_markers

        except Exception as e:
            cutplan.status = "draft"
            db.commit()
            raise

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
        return cutplan
