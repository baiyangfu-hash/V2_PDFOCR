from __future__ import annotations

import statistics
from typing import List, Literal, Tuple

from eip.ingest.pdf_backend import PagePrimitives
from eip.ir.schema import BoundingBox, PageModalityProfile, RawVectorLine


class PDFModalityProfiler:
    """
    L1 PDF Modality Profiler & Characteristic Geometric Scale Estimator (P0-01).
    1. Classifies page modality into VECTOR, RASTER, or HYBRID.
    2. Detects garbled ToUnicode text spans requiring OCR fallback.
    3. Dynamically estimates characteristic contact spacing (W_contact) and stroke width (W_stroke)
       to compute page-adaptive geometric tolerances instead of hardcoding 1.5pt.
    """

    def profile_page(self, prims: PagePrimitives) -> PageModalityProfile:
        page_area = max(1.0, prims.width * prims.height)
        img_area = sum(b.width * b.height for b in prims.image_bboxes)
        raster_ratio = min(1.0, img_area / page_area)

        garbled_bboxes: List[BoundingBox] = [
            t.bbox for t in prims.texts if t.has_garbled_glyphs
        ]
        garbled_count = len(garbled_bboxes)
        vec_count = len(prims.lines) + len(prims.rects) + len(prims.curves)

        modality: Literal["VECTOR", "RASTER", "HYBRID"]
        if vec_count >= 25 and raster_ratio <= 0.15 and garbled_count == 0:
            modality = "VECTOR"
        elif vec_count < 10 and raster_ratio >= 0.60:
            modality = "RASTER"
        else:
            modality = "HYBRID"

        w_contact = self._estimate_contact_width(prims.lines)
        w_stroke = self._estimate_stroke_width(prims.lines)

        # Dynamic dimensionless tolerance formulas per EIP V2 Section 4.1
        eps_collinear = max(1.5 * w_stroke, 0.12 * w_contact)
        eps_snap = max(2.0 * w_stroke, 0.15 * w_contact)
        eps_guard = 0.65 * w_contact

        return PageModalityProfile(
            doc_sha256=prims.doc_sha256,
            page_no=prims.page_no,
            page_width=prims.width,
            page_height=prims.height,
            modality=modality,
            vector_line_count=len(prims.lines),
            text_span_count=len(prims.texts),
            garbled_span_count=garbled_count,
            raster_coverage_ratio=round(raster_ratio, 4),
            characteristic_contact_width_pt=round(w_contact, 3),
            characteristic_stroke_width_pt=round(w_stroke, 3),
            tolerance_collinear_pt=round(eps_collinear, 3),
            tolerance_snap_pt=round(eps_snap, 3),
            tolerance_symbol_guard_pt=round(eps_guard, 3),
            garbled_bboxes=garbled_bboxes,
        )

    @staticmethod
    def _estimate_contact_width(lines: List[RawVectorLine]) -> float:
        """
        Finds pairs of short vertical parallel segments with matching Y-centers and heights
        (supporting both real GX Works3 5.82pt contact bars and larger synthetic contact bars),
        and returns the median horizontal spacing as the characteristic contact width W_contact.
        """
        vert_bars: List[Tuple[float, float, float]] = []  # (x, y_center, height)
        for ln in lines:
            dx = abs(ln.x1 - ln.x0)
            dy = abs(ln.y1 - ln.y0)
            if dx <= 0.5 and 4.2 <= dy <= 28.0:
                x_mid = 0.5 * (ln.x0 + ln.x1)
                y_mid = 0.5 * (ln.y0 + ln.y1)
                vert_bars.append((x_mid, y_mid, dy))

        vert_bars.sort(key=lambda item: (round(item[1], 1), item[0]))
        spacings: List[float] = []
        n = len(vert_bars)
        for i in range(n):
            x_i, y_i, h_i = vert_bars[i]
            for j in range(i + 1, n):
                x_j, y_j, h_j = vert_bars[j]
                if abs(y_j - y_i) <= 1.2 and abs(h_j - h_i) / max(h_i, 1.0) <= 0.15:
                    dist_x = abs(x_j - x_i)
                    if 2.5 <= dist_x <= 15.0:
                        spacings.append(dist_x)
                        break

        if not spacings:
            return 5.82  # Standard GX Works3 fallback if page has no contacts
        return float(statistics.median(spacings))

    @staticmethod
    def _estimate_stroke_width(lines: List[RawVectorLine]) -> float:
        widths = [ln.stroke_width for ln in lines if ln.stroke_width > 0.05]
        if not widths:
            return 0.42
        return float(statistics.median(widths))
