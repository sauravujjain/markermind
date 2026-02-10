from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session
import io
import csv

from ...database import get_db
from ...models import User, Cutplan, CutplanMarker, Order
from ..deps import get_current_user

router = APIRouter(prefix="/export", tags=["exports"])


@router.get("/cutplan/{cutplan_id}/docket")
async def export_cutting_docket(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export cutting docket as PDF."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    # TODO: Generate PDF using ReportLab or WeasyPrint
    # For now, return a simple text representation
    content = f"""
CUTTING DOCKET
==============
Order: {cutplan.order.order_number}
Cutplan: {cutplan.name}
Status: {cutplan.status}

SUMMARY
-------
Total Yards: {cutplan.total_yards:.2f}
Unique Markers: {cutplan.unique_markers}
Total Plies: {cutplan.total_plies}
Total Cost: ${cutplan.total_cost:.2f}

MARKERS
-------
"""
    for marker in cutplan.markers:
        content += f"""
Ratio: {marker.ratio_str}
Efficiency: {marker.efficiency:.1f}%
Length: {marker.length_yards:.2f} yards
Total Plies: {marker.total_plies}
Cuts: {marker.cuts}
Plies by Color: {marker.plies_by_color}
---
"""

    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=docket_{cutplan_id}.txt"}
    )


@router.get("/cutplan/{cutplan_id}/csv")
async def export_cutplan_csv(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export cutplan as CSV."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "Marker #",
        "Ratio",
        "Efficiency %",
        "Length (yards)",
        "Total Plies",
        "Cuts",
        "Plies by Color"
    ])

    # Data
    for i, marker in enumerate(cutplan.markers, 1):
        plies_str = ", ".join(f"{k}:{v}" for k, v in (marker.plies_by_color or {}).items())
        writer.writerow([
            i,
            marker.ratio_str,
            f"{marker.efficiency:.1f}" if marker.efficiency else "",
            f"{marker.length_yards:.2f}" if marker.length_yards else "",
            marker.total_plies or "",
            marker.cuts or "",
            plies_str
        ])

    # Summary row
    writer.writerow([])
    writer.writerow(["SUMMARY"])
    writer.writerow(["Total Yards", f"{cutplan.total_yards:.2f}" if cutplan.total_yards else ""])
    writer.writerow(["Total Plies", cutplan.total_plies or ""])
    writer.writerow(["Unique Markers", cutplan.unique_markers or ""])
    writer.writerow(["Total Cost", f"${cutplan.total_cost:.2f}" if cutplan.total_cost else ""])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=cutplan_{cutplan_id}.csv"}
    )


@router.get("/markers/{marker_id}/dxf")
async def export_marker_dxf(
    marker_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export marker layout as DXF."""
    # TODO: Implement DXF export
    # This would require storing the actual marker layout (piece positions)
    # and converting back to DXF format

    raise HTTPException(
        status_code=501,
        detail="DXF export not yet implemented"
    )


@router.get("/order/{order_id}/summary")
async def export_order_summary(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export order summary including all cutplans."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    output = io.StringIO()
    writer = csv.writer(output)

    # Order info
    writer.writerow(["ORDER SUMMARY"])
    writer.writerow(["Order Number", order.order_number])
    writer.writerow(["Status", order.status])
    writer.writerow([])

    # Demand
    writer.writerow(["DEMAND"])
    size_codes = set()
    for color in order.colors:
        for sq in color.size_quantities:
            size_codes.add(sq.size_code)
    size_codes = sorted(size_codes)

    writer.writerow(["Color"] + size_codes + ["Total"])
    for color in order.colors:
        qty_by_size = {sq.size_code: sq.quantity for sq in color.size_quantities}
        row = [color.color_code]
        total = 0
        for size in size_codes:
            qty = qty_by_size.get(size, 0)
            row.append(qty)
            total += qty
        row.append(total)
        writer.writerow(row)

    writer.writerow([])

    # Cutplans
    writer.writerow(["CUTPLANS"])
    writer.writerow(["Name", "Status", "Efficiency", "Yards", "Markers", "Cost"])
    for cp in order.cutplans:
        writer.writerow([
            cp.name or "",
            cp.status,
            f"{cp.efficiency:.1f}%" if cp.efficiency else "",
            f"{cp.total_yards:.2f}" if cp.total_yards else "",
            cp.unique_markers or "",
            f"${cp.total_cost:.2f}" if cp.total_cost else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=order_{order.order_number}_summary.csv"}
    )
