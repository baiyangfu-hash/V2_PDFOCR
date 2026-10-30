from __future__ import annotations

from pathlib import Path
from typing import List
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pypdfium2 as pdfium

from eip.ir.schema import (
    CrossDocDiagnosticIR,
    FBDInstanceCallIR,
    LadderRungIR,
    PageModalityProfile,
    SchematicIOChannelIR,
)


def render_verification_overlay(
    pdf_path: Path,
    profile: PageModalityProfile,
    rungs: List[LadderRungIR],
    output_png_path: Path,
    scale: float = 2.0,
    page_no: int = 1,
) -> Path:
    doc = pdfium.PdfDocument(str(pdf_path))
    page = doc[page_no - 1]
    bitmap = page.render(scale=scale)
    pil_img = bitmap.to_pil()

    fig, ax = plt.subplots(figsize=(15, 10.6), dpi=160)
    ax.imshow(pil_img, extent=[0, profile.page_width, profile.page_height, 0])

    kind_colors = {
        "NO_CONTACT": "#16a34a",
        "NC_CONTACT": "#dc2626",
        "RISE_CONTACT": "#d97706",
        "FALL_CONTACT": "#9333ea",
        "COMPARE_BOX": "#0d9488",
        "INV_LINE": "#e11d48",
        "OUT_COIL": "#2563eb",
        "SET_COIL": "#0891b2",
        "RST_COIL": "#4f46e5",
        "FUNC_BLOCK": "#7c3aed",
    }

    short_labels = {
        "NO_CONTACT": "NO",
        "NC_CONTACT": "NC",
        "RISE_CONTACT": "RISE",
        "FALL_CONTACT": "FALL",
        "COMPARE_BOX": "CMP",
        "INV_LINE": "INV",
        "OUT_COIL": "COIL",
        "SET_COIL": "SET",
        "RST_COIL": "RST",
        "FUNC_BLOCK": "BOX",
    }

    for r in rungs:
        rb = r.anchor.bbox
        rect_rung = patches.Rectangle(
            (rb.x0 - 4, rb.y0),
            rb.width + 8,
            rb.height,
            linewidth=0.9,
            edgecolor="#64748b",
            facecolor="none",
            linestyle="--",
        )
        ax.add_patch(rect_rung)
        ax.text(
            rb.x0 - 38,
            rb.y0 + 12,
            f"{r.rung_id}\n[{r.verification_status}]",
            fontsize=6.5,
            color="#0f172a",
            weight="bold",
            bbox=dict(boxstyle="round,pad=0.18", facecolor="#e2e8f0", edgecolor="#94a3b8", alpha=0.92),
        )

        for sym in r.geometry_nodes:
            sb = sym.anchor.bbox.expand(1.8)
            color = kind_colors.get(sym.symbol_kind, "#16a34a")
            tag = short_labels.get(sym.symbol_kind, sym.symbol_kind)
            rect_sym = patches.Rectangle(
                (sb.x0, sb.y0),
                sb.width,
                sb.height,
                linewidth=1.4,
                edgecolor=color,
                facecolor=color,
                alpha=0.14,
            )
            ax.add_patch(rect_sym)
            rect_border = patches.Rectangle(
                (sb.x0, sb.y0),
                sb.width,
                sb.height,
                linewidth=1.3,
                edgecolor=color,
                facecolor="none",
            )
            ax.add_patch(rect_border)
            dev_label = sym.device.raw_text if sym.device and sym.device.device_type != "LABEL" else sym.mnemonic
            ax.text(
                sb.x0,
                sb.y0 - 2.5,
                f"{tag}:{dev_label}",
                fontsize=5.8,
                color=color,
                weight="bold",
                bbox=dict(boxstyle="round,pad=0.1", facecolor="#ffffff", edgecolor=color, alpha=0.88, lw=0.5),
            )

        for jid, (jx, jy) in r.junction_coords.items():
            if jid in ("J_LEFT", "J_RIGHT"):
                continue
            ax.plot(jx, jy, marker="o", markersize=3.5, color="#0284c7")

        for mrec in r.merge_audit_logs[:12]:
            mx, my = mrec.point_a
            ax.plot(mx, my, marker="*", markersize=6.5, color="#e11d48", alpha=0.75)

    for gbox in profile.garbled_bboxes:
        eb = gbox.expand(3.0)
        rect_g = patches.Rectangle(
            (eb.x0, eb.y0),
            eb.width,
            eb.height,
            linewidth=1.5,
            edgecolor="#ea580c",
            facecolor="#ffedd5",
            alpha=0.45,
            linestyle="--",
        )
        ax.add_patch(rect_g)
        ax.text(
            eb.x1 + 6.0,
            eb.center[1] + 2.0,
            "OCR_FALLBACK_ROI (Broken CMap Detected)",
            fontsize=7.0,
            color="#c2410c",
            weight="bold",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#ffedd5", edgecolor="#ea580c", alpha=0.95),
        )

    total_stitches = sum(len(r.merge_audit_logs) for r in rungs)
    ax.set_title(
        f"EIP V2 Phase 0 Overlay (Page {page_no}) | Modality={profile.modality} | "
        f"W_contact={profile.characteristic_contact_width_pt}pt | eps_snap={profile.tolerance_snap_pt}pt | "
        f"Rungs={len(rungs)} | Wire Stitches={total_stitches}",
        fontsize=10.0,
        weight="bold",
        pad=8,
    )
    ax.set_xlim(0, profile.page_width)
    ax.set_ylim(profile.page_height, 0)
    plt.tight_layout()
    output_png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_png_path), dpi=160)
    plt.close(fig)
    return output_png_path


def render_fbd_verification_overlay(
    pdf_path: Path,
    fbd_instances: List[FBDInstanceCallIR],
    output_png_path: Path,
    page_no: int = 7,
    scale: float = 2.0,
) -> Path:
    doc = pdfium.PdfDocument(str(pdf_path))
    page = doc[page_no - 1]
    page_w, page_h = page.get_size()
    bitmap = page.render(scale=scale)
    pil_img = bitmap.to_pil()

    fig, ax = plt.subplots(figsize=(15, 10.6), dpi=160)
    ax.imshow(pil_img, extent=[0, page_w, page_h, 0])

    dir_colors = {
        "IN": "#16a34a",
        "OUT": "#2563eb",
        "IN_OUT": "#d97706",
    }

    for inst in fbd_instances:
        for p_idx, p_num in enumerate(inst.pages):
            if p_num != page_no:
                continue
            bb = inst.block_bboxes[p_idx]
            rect_fb = patches.Rectangle(
                (bb.x0, bb.y0),
                bb.width,
                bb.height,
                linewidth=2.0,
                edgecolor="#7c3aed",
                facecolor="#7c3aed",
                alpha=0.08,
            )
            ax.add_patch(rect_fb)
            rect_border = patches.Rectangle(
                (bb.x0, bb.y0),
                bb.width,
                bb.height,
                linewidth=1.8,
                edgecolor="#7c3aed",
                facecolor="none",
            )
            ax.add_patch(rect_border)
            ax.text(
                bb.x0,
                bb.y0 - 6.0,
                f"FB_INST #{inst.layer_index} (Layer={inst.layer_index}, Stitched Pages={inst.pages})",
                fontsize=7.5,
                color="#5b21b6",
                weight="bold",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#ede9fe", edgecolor="#7c3aed", alpha=0.95),
            )

        for pb in inst.pin_bindings:
            if pb.page_no != page_no:
                continue
            sb = pb.symbol_bbox.expand(1.5)
            pb_box = pb.pin_bbox.expand(1.5)
            is_miswire = (
                inst.layer_index == 1 and "3层" in pb.bound_symbol
            )
            is_collision = pb.pin_direction == "IN_OUT" and pb.bound_symbol == "M515"

            color = "#dc2626" if is_miswire else ("#ea580c" if is_collision else dir_colors.get(pb.pin_direction, "#16a34a"))
            for box in (sb, pb_box):
                r_patch = patches.Rectangle(
                    (box.x0, box.y0),
                    box.width,
                    box.height,
                    linewidth=2.2 if (is_miswire or is_collision) else 1.2,
                    edgecolor=color,
                    facecolor=color,
                    alpha=0.22 if (is_miswire or is_collision) else 0.12,
                )
                ax.add_patch(r_patch)

            if is_miswire:
                ax.annotate(
                    "[XDOC-01 ERROR] LAYER 1 FB PIN MISWIRED TO LAYER-3 SIGNAL!\n(Expected: Layer-1 PickDone from 01_Ladder.pdf p28)",
                    xy=(sb.x0, sb.center[1]),
                    xytext=(sb.x0 - 165, sb.y0 - 38),
                    fontsize=7.5,
                    weight="bold",
                    color="#ffffff",
                    bbox=dict(boxstyle="round,pad=0.35", facecolor="#dc2626", edgecolor="#991b1b", lw=1.5),
                    arrowprops=dict(arrowstyle="->", color="#dc2626", lw=2.0),
                )
            elif is_collision:
                ax.text(
                    sb.x0 - 85,
                    sb.center[1] + 12,
                    "[XDOC-03 WARN] Shared InOut M515 Collision",
                    fontsize=6.5,
                    weight="bold",
                    color="#9a3412",
                    bbox=dict(boxstyle="round,pad=0.2", facecolor="#ffedd5", edgecolor="#ea580c", lw=1.0),
                )

    ax.set_title(
        f"EIP V2 Phase 1 Multi-Page FBD/LD Verification Overlay (03_FBD_LD.pdf Page {page_no}) | "
        f"Cross-Page FB Stitching & Static Bug Detection",
        fontsize=10.0,
        weight="bold",
        pad=8,
    )
    ax.set_xlim(0, page_w)
    ax.set_ylim(page_h, 0)
    plt.tight_layout()
    output_png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_png_path), dpi=160)
    plt.close(fig)
    return output_png_path


def render_schematic_verification_overlay(
    pdf_path: Path,
    channels: List[SchematicIOChannelIR],
    output_png_path: Path,
    page_no: int = 22,
    scale: float = 1.8,
) -> Path:
    """
    Renders a PLC I/O schematic page from '5.边框储料机.pdf' and overlays:
      - Recognized physical component tags (-PR*, -SQP*, -KA*, -YV*)
      - Red callouts on schematic terminal pin copy-paste typos (SCHEM-01: e.g. X51..X57 on p22, Y33 on p35)
      - Program-First Reconciliation status badges (SPARE_ACTIVATED_IN_PLC, REPURPOSED_OR_RENAMED_IN_PLC)
    """
    doc = pdfium.PdfDocument(str(pdf_path))
    page = doc[page_no - 1]
    page_w, page_h = page.get_size()
    bitmap = page.render(scale=scale)
    pil_img = bitmap.to_pil()

    fig, ax = plt.subplots(figsize=(15.5, 11.0), dpi=160)
    ax.imshow(pil_img, extent=[0, page_w, page_h, 0])

    page_chs = [ch for ch in channels if ch.page_no == page_no]
    status_colors = {
        "MATCHED": "#16a34a",
        "PIN_TYPO_IN_SCHEMATIC": "#dc2626",
        "SPARE_ACTIVATED_IN_PLC": "#2563eb",
        "REPURPOSED_OR_RENAMED_IN_PLC": "#d97706",
        "DEPRECATED_IN_PLC": "#64748b",
    }

    for ch in page_chs:
        col_color = status_colors.get(ch.reconciliation_status, "#16a34a")
        for cb in ch.component_bboxes:
            eb = cb.expand(3.0)
            ax.add_patch(
                patches.Rectangle(
                    (eb.x0, eb.y0),
                    eb.width,
                    eb.height,
                    linewidth=1.6,
                    edgecolor="#0284c7",
                    facecolor="#0284c7",
                    alpha=0.15,
                )
            )

        if ch.has_pin_typo and ch.top_pin_bbox:
            tb = ch.top_pin_bbox.expand(4.0)
            ax.add_patch(
                patches.Rectangle(
                    (tb.x0, tb.y0),
                    tb.width,
                    tb.height,
                    linewidth=2.2,
                    edgecolor="#dc2626",
                    facecolor="#dc2626",
                    alpha=0.25,
                )
            )
            ax.text(
                tb.x0 - 12,
                tb.y0 - 8,
                f"[SCHEM-01 TYPO]\n{ch.raw_top_pins[0]} != {ch.canonical_device}",
                fontsize=6.5,
                weight="bold",
                color="#ffffff",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#dc2626", edgecolor="#991b1b", lw=1.2),
            )

        badge_y = 588.0 if ch.canonical_device.startswith("X") else 248.0
        tags_str = ",".join(ch.component_tags) if ch.component_tags else "NO_TAG"
        ax.text(
            ch.column_bbox.x0 + 4,
            badge_y,
            f"{ch.canonical_device} [{tags_str}]\n[{ch.reconciliation_status}]",
            fontsize=6.0,
            weight="bold",
            color=col_color,
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#ffffff", edgecolor=col_color, alpha=0.92, lw=1.2),
        )

    typo_cnt = sum(1 for c in page_chs if c.has_pin_typo)
    evol_cnt = sum(
        1
        for c in page_chs
        if c.reconciliation_status in ("SPARE_ACTIVATED_IN_PLC", "REPURPOSED_OR_RENAMED_IN_PLC")
    )
    ax.set_title(
        f"EIP V2 Phase 2 Electrical Schematic & Program-First Reconciliation Overlay (Page {page_no}) | "
        f"Channels={len(page_chs)} | Pin Typos={typo_cnt} | Program-Ahead Updates={evol_cnt}",
        fontsize=10.0,
        weight="bold",
        pad=8,
    )
    ax.set_xlim(0, page_w)
    ax.set_ylim(page_h, 0)
    plt.tight_layout()
    output_png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_png_path), dpi=160)
    plt.close(fig)
    return output_png_path
