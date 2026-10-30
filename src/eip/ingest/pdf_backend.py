from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

from eip.ir.schema import (
    BoundingBox,
    RawTextSpan,
    RawVectorCurve,
    RawVectorLine,
    RawVectorRect,
)

_CID_GARBLED_RE = re.compile(r"\(cid:[0-9]+\)", re.IGNORECASE)


class PagePrimitives:
    """Container for extracted raw vector primitives and text spans on a single PDF page."""

    def __init__(
        self,
        doc_sha256: str,
        page_no: int,
        width: float,
        height: float,
        lines: List[RawVectorLine],
        rects: List[RawVectorRect],
        curves: List[RawVectorCurve],
        texts: List[RawTextSpan],
        image_bboxes: List[BoundingBox],
    ) -> None:
        self.doc_sha256 = doc_sha256
        self.page_no = page_no
        self.width = width
        self.height = height
        self.lines = lines
        self.rects = rects
        self.curves = curves
        self.texts = texts
        self.image_bboxes = image_bboxes


class PdfBackend(Protocol):
    """Unified PDF Extraction Protocol supporting PyMuPDF (primary) and pdfplumber (fallback)."""

    def extract_page_primitives(self, pdf_path: Path, page_no: int = 1) -> PagePrimitives:
        ...


def compute_file_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def has_garbled_characters(text: str) -> bool:
    """
    Detects broken ToUnicode CMap / unmapped glyphs:
      - Unicode replacement character U+FFFD or NUL
      - pdfminer/pdfplumber unmapped glyph marker '(cid:1234)'
      - Unicode Private Use Area (U+E000..U+F8FF) or C0/C1 control characters
    """
    if "\ufffd" in text or "\x00" in text:
        return True
    if _CID_GARBLED_RE.search(text):
        return True
    for ch in text:
        code = ord(ch)
        if (
            (0xE000 <= code <= 0xF8FF)
            or (0x007F <= code <= 0x009F)
            or (code < 32 and ch not in ("\t", "\n", "\r"))
        ):
            return True
    return False


class PyMuPDFBackend:
    """Primary high-speed vector extractor using PyMuPDF (fitz)."""

    def extract_page_primitives(self, pdf_path: Path, page_no: int = 1) -> PagePrimitives:
        import fitz  # type: ignore

        doc_sha = compute_file_sha256(pdf_path)
        doc = fitz.open(str(pdf_path))
        page = doc[page_no - 1]
        width = float(page.rect.width)
        height = float(page.rect.height)

        lines: List[RawVectorLine] = []
        rects: List[RawVectorRect] = []
        curves: List[RawVectorCurve] = []

        drawings = page.get_drawings()
        line_idx = 0
        rect_idx = 0
        curve_idx = 0

        for d in drawings:
            stroke_w = float(d.get("width") or 0.5)
            is_filled = bool(d.get("fill") is not None)
            items = d.get("items", [])
            has_bezier = any(it[0] in ("c", "qu") for it in items)

            # Check for filled 4-point arrowhead polygons in GX Works3
            if is_filled and not has_bezier and all(it[0] == "l" for it in items):
                poly_pts: List[Tuple[float, float]] = []
                for it in items:
                    p1, p2 = it[1], it[2]
                    if not poly_pts:
                        poly_pts.append((float(p1.x), float(p1.y)))
                    poly_pts.append((float(p2.x), float(p2.y)))
                if len(poly_pts) >= 3:
                    xs = [p[0] for p in poly_pts]
                    ys = [p[1] for p in poly_pts]
                    curve_idx += 1
                    curves.append(
                        RawVectorCurve(
                            curve_id=f"C_p{page_no}_{curve_idx}",
                            page_no=page_no,
                            points=poly_pts,
                            bbox=BoundingBox(
                                x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)
                            ),
                            stroke_width=stroke_w,
                            is_filled=True,
                        )
                    )
                    continue

            for item in items:
                op = item[0]
                if op == "l":
                    p1, p2 = item[1], item[2]
                    line_idx += 1
                    lines.append(
                        RawVectorLine(
                            line_id=f"L_p{page_no}_{line_idx}",
                            page_no=page_no,
                            x0=float(p1.x),
                            y0=float(p1.y),
                            x1=float(p2.x),
                            y1=float(p2.y),
                            stroke_width=stroke_w,
                        )
                    )
                elif op == "re":
                    r = item[1]
                    rect_idx += 1
                    rects.append(
                        RawVectorRect(
                            rect_id=f"R_p{page_no}_{rect_idx}",
                            page_no=page_no,
                            bbox=BoundingBox(
                                x0=float(r.x0),
                                y0=float(r.y0),
                                x1=float(r.x1),
                                y1=float(r.y1),
                            ),
                            stroke_width=stroke_w,
                            is_filled=is_filled,
                        )
                    )
                elif op in ("c", "qu"):
                    pts = [(float(p.x), float(p.y)) for p in item[1:]]
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    curve_idx += 1
                    curves.append(
                        RawVectorCurve(
                            curve_id=f"C_p{page_no}_{curve_idx}",
                            page_no=page_no,
                            points=pts,
                            bbox=BoundingBox(
                                x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)
                            ),
                            stroke_width=stroke_w,
                            is_filled=is_filled,
                        )
                    )

        texts: List[RawTextSpan] = []
        image_bboxes: List[BoundingBox] = []
        text_dict = page.get_text("dict")
        t_idx = 0
        for block in text_dict.get("blocks", []):
            if block.get("type") == 1:
                b = block.get("bbox", [0, 0, 0, 0])
                image_bboxes.append(
                    BoundingBox(x0=float(b[0]), y0=float(b[1]), x1=float(b[2]), y1=float(b[3]))
                )
            elif block.get("type") == 0:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = str(span.get("text", "")).strip()
                        if not txt:
                            continue
                        t_idx += 1
                        b = span.get("bbox", [0, 0, 0, 0])
                        texts.append(
                            RawTextSpan(
                                text_id=f"T_p{page_no}_{t_idx}",
                                page_no=page_no,
                                text=txt,
                                bbox=BoundingBox(
                                    x0=float(b[0]),
                                    y0=float(b[1]),
                                    x1=float(b[2]),
                                    y1=float(b[3]),
                                ),
                                font_name=str(span.get("font", "Unknown")),
                                font_size=float(span.get("size", 8.0)),
                                has_garbled_glyphs=has_garbled_characters(txt),
                            )
                        )
        doc.close()
        return PagePrimitives(
            doc_sha256=doc_sha,
            page_no=page_no,
            width=width,
            height=height,
            lines=lines,
            rects=rects,
            curves=curves,
            texts=texts,
            image_bboxes=image_bboxes,
        )


class PdfPlumberBackend:
    """Permissive MIT-licensed PDF vector extractor using pdfplumber."""

    def extract_page_primitives(self, pdf_path: Path, page_no: int = 1) -> PagePrimitives:
        import pdfplumber

        doc_sha = compute_file_sha256(pdf_path)
        with pdfplumber.open(str(pdf_path)) as pdf:
            page = pdf.pages[page_no - 1]
            width = float(page.width)
            height = float(page.height)

            lines: List[RawVectorLine] = []
            for idx, l_obj in enumerate(page.lines, start=1):
                lines.append(
                    RawVectorLine(
                        line_id=f"L_p{page_no}_{idx}",
                        page_no=page_no,
                        x0=float(l_obj["x0"]),
                        y0=float(l_obj["top"]),
                        x1=float(l_obj["x1"]),
                        y1=float(l_obj["bottom"]),
                        stroke_width=float(l_obj.get("linewidth") or 0.5),
                    )
                )

            rects: List[RawVectorRect] = []
            for idx, r_obj in enumerate(page.rects, start=1):
                rects.append(
                    RawVectorRect(
                        rect_id=f"R_p{page_no}_{idx}",
                        page_no=page_no,
                        bbox=BoundingBox(
                            x0=float(r_obj["x0"]),
                            y0=float(r_obj["top"]),
                            x1=float(r_obj["x1"]),
                            y1=float(r_obj["bottom"]),
                        ),
                        stroke_width=float(r_obj.get("linewidth") or 0.5),
                        is_filled=bool(r_obj.get("fill", False)),
                    )
                )

            curves: List[RawVectorCurve] = []
            for idx, c_obj in enumerate(page.curves, start=1):
                path_cmds = c_obj.get("path", [])
                has_bezier = any(cmd[0] in ("c", "v", "y") for cmd in path_cmds)
                is_filled = bool(c_obj.get("fill", False))
                stroke_w = float(c_obj.get("linewidth") or 0.5)

                all_pts: List[Tuple[float, float]] = []
                for cmd in path_cmds:
                    for pt in cmd[1:]:
                        if isinstance(pt, (tuple, list)) and len(pt) == 2:
                            all_pts.append((float(pt[0]), float(pt[1])))
                if not all_pts:
                    all_pts = [(float(px), float(py)) for px, py in c_obj.get("pts", [])]

                if has_bezier or is_filled:
                    if len(all_pts) >= 2:
                        xs = [p[0] for p in all_pts]
                        ys = [p[1] for p in all_pts]
                        curves.append(
                            RawVectorCurve(
                                curve_id=f"C_p{page_no}_{idx}",
                                page_no=page_no,
                                points=all_pts,
                                bbox=BoundingBox(
                                    x0=min(xs),
                                    y0=min(ys),
                                    x1=max(xs),
                                    y1=max(ys),
                                ),
                                stroke_width=stroke_w,
                                is_filled=is_filled,
                            )
                        )
                else:
                    # Unfilled straight polyline (e.g. GX Works3 3-point power rail or vertical drop line):
                    # decompose into consecutive 2-point RawVectorLine segments
                    for seg_k in range(len(all_pts) - 1):
                        p_a = all_pts[seg_k]
                        p_b = all_pts[seg_k + 1]
                        if math.hypot(p_b[0] - p_a[0], p_b[1] - p_a[1]) > 0.05:
                            lines.append(
                                RawVectorLine(
                                    line_id=f"L_p{page_no}_c{idx}_{seg_k + 1}",
                                    page_no=page_no,
                                    x0=p_a[0],
                                    y0=p_a[1],
                                    x1=p_b[0],
                                    y1=p_b[1],
                                    stroke_width=stroke_w,
                                )
                            )

            raw_words = page.extract_words(
                x_tolerance=1.2,
                y_tolerance=1.5,
                keep_blank_chars=False,
                extra_attrs=["fontname", "size"],
            )
            texts: List[RawTextSpan] = []
            for idx, w in enumerate(raw_words, start=1):
                txt = str(w["text"]).strip()
                if not txt:
                    continue
                texts.append(
                    RawTextSpan(
                        text_id=f"T_p{page_no}_{idx}",
                        page_no=page_no,
                        text=txt,
                        bbox=BoundingBox(
                            x0=float(w["x0"]),
                            y0=float(w["top"]),
                            x1=float(w["x1"]),
                            y1=float(w["bottom"]),
                        ),
                        font_name=str(w.get("fontname", "Unknown")),
                        font_size=float(w.get("size", 8.0)),
                        has_garbled_glyphs=has_garbled_characters(txt),
                    )
                )

            image_bboxes: List[BoundingBox] = []
            for img in page.images:
                image_bboxes.append(
                    BoundingBox(
                        x0=float(img["x0"]),
                        y0=float(img["top"]),
                        x1=float(img["x1"]),
                        y1=float(img["bottom"]),
                    )
                )

            return PagePrimitives(
                doc_sha256=doc_sha,
                page_no=page_no,
                width=width,
                height=height,
                lines=lines,
                rects=rects,
                curves=curves,
                texts=texts,
                image_bboxes=image_bboxes,
            )


def get_default_pdf_backend() -> PdfBackend:
    """Automatically selects PyMuPDFBackend if installed, otherwise PdfPlumberBackend."""
    try:
        import fitz  # noqa: F401

        return PyMuPDFBackend()
    except ImportError:
        return PdfPlumberBackend()
