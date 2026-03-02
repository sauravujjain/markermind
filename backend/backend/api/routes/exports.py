from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session, joinedload
import io
import csv

from ...database import get_db
from ...models import User, Cutplan, CutplanMarker, Order, Pattern, MarkerLayout, PatternFabricMapping
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


@router.get("/order/{order_id}/excel")
async def export_order_excel(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Export all approved/refined cutplans as Excel workbook — one tab per fabric.
    Each fabric tab shows the demand table once, then each cutplan stacked with
    marker tables, totals, and fully recalculated cost breakdowns.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    import math

    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Get canonical size ordering and perimeter data from pattern
    # perimeter_all_materials: {material_name: {size: perimeter_cm}}
    canonical_sizes = None
    perimeter_all_materials = {}
    pattern = None
    if order.pattern_id:
        pattern = db.query(Pattern).filter(Pattern.id == order.pattern_id).first()
        if pattern:
            if pattern.available_sizes:
                canonical_sizes = pattern.available_sizes
            meta = pattern.parse_metadata or {}
            perimeter_all_materials = meta.get("perimeter_by_size", {})

    # Load cost config for customer
    from ...models import CostConfig as CostConfigModel, Fabric as FabricModel
    cost_config = db.query(CostConfigModel).filter(
        CostConfigModel.customer_id == current_user.customer_id
    ).first()

    # Cost config defaults
    fabric_cost_per_yard = cost_config.fabric_cost_per_yard if cost_config else 3.0
    spreading_cost_per_yard = cost_config.spreading_cost_per_yard if cost_config else 0.00122
    spreading_cost_per_ply = cost_config.spreading_cost_per_ply if cost_config else 0.013
    cutting_labor_per_hour = cost_config.cutting_labor_cost_per_hour if cost_config else 1.0
    cutting_workers = cost_config.cutting_workers_per_cut if cost_config else 1
    cutting_speed_cm_s = cost_config.cutting_speed_cm_per_s if cost_config else 10.0
    # cutting_cost_per_cm = (labor_per_hour * workers) / 3600 / speed_cm_per_s
    cutting_cost_per_cm = (cutting_labor_per_hour * cutting_workers) / 3600.0 / cutting_speed_cm_s if cutting_speed_cm_s > 0 else 0.0
    # Prep cost per meter: sum of enabled paper layer costs
    prep_cost_per_meter = 0.0
    if cost_config:
        if cost_config.prep_perf_paper_enabled:
            prep_cost_per_meter += cost_config.prep_perf_paper_cost_per_m or 0.0
        if cost_config.prep_underlayer_enabled:
            prep_cost_per_meter += cost_config.prep_underlayer_cost_per_m or 0.0
        if cost_config.prep_top_layer_enabled:
            prep_cost_per_meter += cost_config.prep_top_layer_cost_per_m or 0.0
    else:
        prep_cost_per_meter = 0.25  # default

    # Default perimeter per bundle if no data
    DEFAULT_PERIMETER_CM_PER_BUNDLE = 2540.0

    # Query ALL cutplans with status approved/refining/refined
    plans = db.query(Cutplan).filter(
        Cutplan.order_id == order_id,
        Cutplan.status.in_(["approved", "refined", "refining"]),
    ).options(
        joinedload(Cutplan.markers).joinedload(CutplanMarker.layout)
    ).all()

    if not plans:
        raise HTTPException(status_code=404, detail="No approved cutplans found for this order")

    # De-duplicate markers from joinedload (SQLAlchemy quirk)
    seen_plan_ids = set()
    unique_plans = []
    for p in plans:
        if p.id not in seen_plan_ids:
            seen_plan_ids.add(p.id)
            unique_plans.append(p)
    plans = unique_plans

    # Group order lines by fabric code
    fabric_lines = {}
    for line in order.order_lines:
        fabric_lines.setdefault(line.fabric_code, []).append(line)

    # Collect all demand sizes across all fabrics (for canonical ordering)
    demand_sizes_set = set()
    seen_colors = set()
    for line in order.order_lines:
        if line.color_code in seen_colors:
            continue
        seen_colors.add(line.color_code)
        for sq in line.size_quantities:
            if sq.quantity > 0:
                demand_sizes_set.add(sq.size_code)

    # Order sizes canonically
    if canonical_sizes:
        sizes = [s for s in canonical_sizes if s in demand_sizes_set]
        for s in sorted(demand_sizes_set):
            if s not in sizes:
                sizes.append(s)
    else:
        sizes = sorted(demand_sizes_set)

    # Styles
    header_font = Font(bold=True, size=11)
    title_font = Font(bold=True, size=14)
    subtitle_font = Font(bold=True, size=12, color="333399")
    header_fill = PatternFill(start_color="E8E0D5", end_color="E8E0D5", fill_type="solid")
    cutplan_fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin'),
    )

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # Remove default sheet

    # Unique fabric codes in order of appearance
    fabric_codes = list(dict.fromkeys(line.fabric_code for line in order.order_lines))

    # Look up fabric cost_per_yard from Fabric table
    fabric_cost_map = {}  # fabric_code -> cost_per_yard
    for fc in fabric_codes:
        fab = db.query(FabricModel).filter(
            FabricModel.customer_id == current_user.customer_id,
            FabricModel.code == fc,
        ).first()
        if fab and fab.cost_per_yard:
            fabric_cost_map[fc] = fab.cost_per_yard

    for fabric_code in fabric_codes:
        sheet_name = fabric_code[:31]  # Excel max 31 chars
        ws = wb.create_sheet(title=sheet_name)

        # Collect color demands for this fabric
        fabric_color_demands = {}  # color -> {size: qty}
        fabric_seen_colors = set()
        for line in fabric_lines.get(fabric_code, []):
            if line.color_code in fabric_seen_colors:
                continue
            fabric_seen_colors.add(line.color_code)
            color_demand = {}
            for sq in line.size_quantities:
                if sq.quantity > 0:
                    color_demand[sq.size_code] = sq.quantity
            if color_demand:
                fabric_color_demands[line.color_code] = color_demand

        # Get fabric-specific cost per yard (fall back to cost config default)
        fc_cost_per_yard = fabric_cost_map.get(fabric_code, fabric_cost_per_yard)

        # Resolve perimeter_by_size for this fabric's material
        # perimeter_all_materials is {material: {size: cm}}, we need {size: cm}
        perimeter_by_size = {}
        if perimeter_all_materials and pattern:
            # Look up PatternFabricMapping to find which material maps to this fabric
            fab = db.query(FabricModel).filter(
                FabricModel.customer_id == current_user.customer_id,
                FabricModel.code == fabric_code,
            ).first()
            if fab:
                mapping = db.query(PatternFabricMapping).filter(
                    PatternFabricMapping.pattern_id == pattern.id,
                    PatternFabricMapping.fabric_id == fab.id,
                ).first()
                if mapping and mapping.material_name in perimeter_all_materials:
                    perimeter_by_size = perimeter_all_materials[mapping.material_name]
            # Fallback: single material — use it directly
            if not perimeter_by_size and len(perimeter_all_materials) == 1:
                perimeter_by_size = list(perimeter_all_materials.values())[0]

        row = 1

        # -- Header --
        ws.cell(row=row, column=1, value=f"Order: {order.order_number}").font = title_font
        row += 1
        ws.cell(row=row, column=1, value=f"Fabric: {fabric_code}")
        row += 2

        # -- Demand Table (once per fabric) --
        ws.cell(row=row, column=1, value="DEMAND").font = header_font
        row += 1

        # Header row
        ws.cell(row=row, column=1, value="Color").font = header_font
        for i, size in enumerate(sizes):
            cell = ws.cell(row=row, column=2 + i, value=size)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = thin_border
            cell.alignment = Alignment(horizontal='center')
        ws.cell(row=row, column=2 + len(sizes), value="Total").font = header_font
        row += 1

        # Color rows
        for color, demand in fabric_color_demands.items():
            ws.cell(row=row, column=1, value=color)
            total = 0
            for i, size in enumerate(sizes):
                qty = demand.get(size, 0)
                cell = ws.cell(row=row, column=2 + i, value=qty if qty else "")
                cell.border = thin_border
                cell.alignment = Alignment(horizontal='center')
                total += qty
            ws.cell(row=row, column=2 + len(sizes), value=total)
            row += 1

        row += 2

        # -- Each cutplan stacked --
        for plan_idx, plan in enumerate(plans):
            # Cutplan header with separator
            plan_label = plan.name or f"Option {plan_idx + 1}"
            cell = ws.cell(row=row, column=1, value=f"CUTPLAN: {plan_label}")
            cell.font = subtitle_font
            cell.fill = cutplan_fill
            # Fill the header row across columns
            for ci in range(2, 2 + len(sizes) + 7):
                ws.cell(row=row, column=ci).fill = cutplan_fill
            row += 1
            ws.cell(row=row, column=1, value=f"Status: {plan.status}")
            row += 2

            # -- Marker Table --
            ws.cell(row=row, column=1, value="MARKERS").font = header_font
            row += 1

            # Header
            marker_headers = ["Marker #"] + sizes + ["Bundles", "Efficiency %", "Length (yd)", "Plies", "Cuts", "Source"]
            for i, h in enumerate(marker_headers):
                cell = ws.cell(row=row, column=1 + i, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.border = thin_border
                cell.alignment = Alignment(horizontal='center')
            row += 1

            # Marker rows
            totals_by_size = {s: 0 for s in sizes}
            total_plies_sum = 0
            total_cuts_sum = 0
            total_fabric_yards = 0.0
            total_cutting_cost = 0.0
            total_prep_cost = 0.0

            for m_idx, marker in enumerate(plan.markers, 1):
                ratios = marker.ratio_str.split("-")
                bundles = sum(int(x) for x in ratios)

                # Determine source and values from CPU layout if available
                has_cpu = marker.layout is not None
                source = "CPU" if has_cpu else "GPU"
                eff = (marker.layout.utilization * 100) if has_cpu and marker.layout.utilization else ((marker.efficiency or 0) * 100)
                length_yd = marker.layout.length_yards if has_cpu and marker.layout.length_yards else (marker.length_yards or 0)
                length_m = length_yd * 0.9144  # yards to meters

                marker_plies = marker.total_plies or 0
                marker_cuts = marker.cuts or 0

                ws.cell(row=row, column=1, value=m_idx).border = thin_border
                for i, size in enumerate(sizes):
                    ratio_val = int(ratios[i]) if i < len(ratios) else 0
                    cell = ws.cell(row=row, column=2 + i, value=ratio_val if ratio_val else "")
                    cell.border = thin_border
                    cell.alignment = Alignment(horizontal='center')
                    totals_by_size[size] += ratio_val * marker_plies

                col_offset = 2 + len(sizes)
                ws.cell(row=row, column=col_offset, value=bundles).border = thin_border
                ws.cell(row=row, column=col_offset + 1, value=round(eff, 1)).border = thin_border
                ws.cell(row=row, column=col_offset + 2, value=round(length_yd, 2)).border = thin_border
                ws.cell(row=row, column=col_offset + 3, value=marker_plies).border = thin_border
                ws.cell(row=row, column=col_offset + 4, value=marker_cuts).border = thin_border
                ws.cell(row=row, column=col_offset + 5, value=source).border = thin_border

                total_plies_sum += marker_plies
                total_cuts_sum += marker_cuts
                total_fabric_yards += length_yd * marker_plies

                # Cutting cost: marker_perimeter_cm * cuts * cutting_cost_per_cm
                marker_perimeter_cm = 0.0
                for i, size in enumerate(sizes):
                    ratio_val = int(ratios[i]) if i < len(ratios) else 0
                    if ratio_val > 0:
                        size_perim = perimeter_by_size.get(size, DEFAULT_PERIMETER_CM_PER_BUNDLE)
                        marker_perimeter_cm += size_perim * ratio_val
                total_cutting_cost += marker_perimeter_cm * marker_cuts * cutting_cost_per_cm

                # Prep cost: length_m * cuts * prep_cost_per_meter
                total_prep_cost += length_m * marker_cuts * prep_cost_per_meter

                row += 1

            # Totals row
            ws.cell(row=row, column=1, value="TOTAL").font = header_font
            for i, size in enumerate(sizes):
                cell = ws.cell(row=row, column=2 + i, value=totals_by_size[size])
                cell.font = header_font
                cell.border = thin_border
                cell.alignment = Alignment(horizontal='center')
            col_offset = 2 + len(sizes)
            ws.cell(row=row, column=col_offset + 3, value=total_plies_sum).font = header_font
            ws.cell(row=row, column=col_offset + 4, value=total_cuts_sum).font = header_font
            row += 2

            # -- Summary --
            ws.cell(row=row, column=1, value=f"Total Yards: {total_fabric_yards:.2f}").font = header_font
            row += 2

            # -- Cost Breakdown (fully recalculated) --
            ws.cell(row=row, column=1, value="COST BREAKDOWN").font = header_font
            row += 1

            # Fabric cost
            recalc_fabric_cost = total_fabric_yards * fc_cost_per_yard

            # Spreading cost
            recalc_spreading_cost = (total_fabric_yards * spreading_cost_per_yard) + (total_plies_sum * spreading_cost_per_ply)

            cost_items = [
                ("Fabric Cost", recalc_fabric_cost),
                ("Spreading Cost", recalc_spreading_cost),
                ("Cutting Cost", total_cutting_cost),
                ("Prep Cost", total_prep_cost),
            ]
            cost_total = sum(v for _, v in cost_items)
            cost_items.append(("Total Cost", cost_total))

            for label, value in cost_items:
                ws.cell(row=row, column=1, value=label)
                ws.cell(row=row, column=2, value=f"${value:.2f}")
                row += 1

            row += 2  # Blank rows between cutplans

        # Auto-fit column widths
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = min(max_length + 3, 20)

    # Write to bytes buffer
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"order_{order.order_number}_cutplan.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
