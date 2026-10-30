from __future__ import annotations

from pathlib import Path
from typing import List
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pypdfium2 as pdfium

from eip.ir.schema import LadderRungIR, PageModalityProfile


def render_verification_overlay(
    pdf_path: Path,
    profile: PageModalityProfile,
    rungs: List[LadderRungIR],
    output_png_path: Path,
    scale: float = 2.0,
    page_no: int = 1,
) -> Path:
    """
    Renders the raw PDF page via pypdfium2 and overlays:
      - Recognized LadderSymbolNode bounding boxes (color-coded by symbol_kind)
      - Electrical Junction Nodes (J_LEFT, J_RIGHT, internal junctions)
      - Adaptive endpoint/collinear stitch audit locations (GeometryMergeRecord)
      - Garbled ToUnicode CMap ROI boxes flagged for PaddleOCR fallback
    """
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

        # Plot up to 12 representative stitch points per rung to avoid clutter on dense 22-column grids
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
