from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from eip.ingest.pdf_backend import PagePrimitives
from eip.ir.schema import (
    BoundingBox,
    EvidenceAnchor,
    GeometryEdge,
    GeometryMergeRecord,
    LadderRungIR,
    LadderSymbolNode,
    PageModalityProfile,
    RawTextSpan,
    RawVectorCurve,
    RawVectorLine,
    RawVectorRect,
    SourceModality,
)
from eip.plc.lexer import FX5UDeviceLexer

_COMPARE_MNEMONICS = {
    "=",
    "<>",
    ">",
    "<",
    ">=",
    "<=",
    "D=",
    "D<>",
    "D>",
    "D<",
    "D>=",
    "D<=",
    "E=",
    "E<>",
    "E>",
    "E<",
    "E>=",
    "E<=",
    "$=",
    "$<>",
    "LD=",
    "LD<>",
    "LD>",
    "LD<",
    "LD>=",
    "LD<=",
    "AND=",
    "AND<>",
    "AND>",
    "AND<",
    "AND>=",
    "AND<=",
    "OR=",
    "OR<>",
    "OR>",
    "OR<",
    "OR>=",
    "OR<=",
}

_TERMINAL_OUTPUT_KINDS = {"OUT_COIL", "SET_COIL", "RST_COIL", "FUNC_BLOCK"}


class DisjointSet:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> bool:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            if ri < rj:
                self.parent[rj] = ri
            else:
                self.parent[ri] = rj
            return True
        return False


class LadderGeometryBuilder:
    """
    P0-03: Deterministic Vector Ladder Geometry & Topology Builder (V0.3 Real GX Works3 + Synthetic).
    - Identifies true Left/Right Power Rails by aggregating collinear vertical segments per X-coordinate
      (handling GX Works3 per-row segmented power rails at x=122.93 and x=770.99 as well as single-line rails)
    - Recognizes:
      * NO/NC/RISE/FALL contacts (both 5.82pt real GX Works3 contacts with filled 4-pt arrow polygons and synthetic contacts)
      * Standalone INV diagonal slashes ('/') on horizontal wires (splitting the wire to insert an INV_LINE edge)
      * Output coils (both real GX Works3 5.82pt closed Bezier circles and synthetic 2-arc coils)
      * Multi-cell gray-header GX Works3 boxes (distinguishing inline COMPARE_BOX from right-hand FUNC_BLOCK/SET/RST/OUT)
        and synthetic single-rect boxes, snapping port_y directly to the incident conducting wire Y
    - Two-pass text binding:
      * Pass 1 binds all device addresses above contacts/coils first (so comments never steal next-row device tokens)
      * Pass 2 binds multi-line Chinese comments below contacts/coils
    - Uses Shapely STRtree + Adaptive Tolerance (eps_snap, eps_collinear, eps_guard)
      to stitch 0.42pt per-cell GX Works3 micro-gaps, cluster wire junctions, and build the directed Geometry Graph
    """

    def __init__(self, lexer: Optional[FX5UDeviceLexer] = None) -> None:
        self.lexer = lexer or FX5UDeviceLexer()

    def build_page_rungs(
        self, prims: PagePrimitives, profile: PageModalityProfile
    ) -> List[LadderRungIR]:
        x_left, x_right, y_rail_min, y_rail_max, rail_ids = self._find_power_rails(
            prims.lines, prims.rects, prims.height
        )
        used_line_ids: Set[str] = set(rail_ids)
        used_text_ids: Set[str] = set()

        for t in prims.texts:
            tcy = t.bbox.center[1]
            tcx = t.bbox.center[0]
            if (
                tcy < y_rail_min - 20.0
                or tcy > y_rail_max + 20.0
                or tcx < x_left - 2.0
                or tcx > x_right + 2.0
            ):
                used_text_ids.add(t.text_id)

        # 1. Detect box instructions
        box_symbols, box_rect_ids, box_line_ids = self._detect_box_instructions(
            prims, x_left, x_right, y_rail_min, y_rail_max, used_text_ids
        )
        used_line_ids.update(box_line_ids)

        # 2. Detect output coils (Pass 1: geometry + device above)
        coil_symbols = self._detect_coils(
            prims, x_left, x_right, y_rail_min, y_rail_max, used_text_ids
        )

        # 3. Detect contacts (Pass 1: geometry + device above)
        contact_symbols, contact_line_ids = self._detect_contacts(
            prims,
            profile,
            x_left,
            x_right,
            y_rail_min,
            y_rail_max,
            used_line_ids,
            used_text_ids,
        )
        used_line_ids.update(contact_line_ids)

        # 4. Pass 2 Text Binding: bind comments below contacts, coils, and synthetic boxes
        for sym in contact_symbols + coil_symbols + box_symbols:
            if sym.comment_text is None:
                sym.comment_text = self._find_comment_below(
                    sym.anchor.bbox, prims.texts, used_text_ids
                )

        # 5. Detect standalone INV slashes ('/') on horizontal wires
        inv_symbols, inv_line_ids = self._detect_inv_slashes(
            prims, x_left, x_right, y_rail_min, y_rail_max, used_line_ids
        )
        used_line_ids.update(inv_line_ids)

        all_symbols = contact_symbols + inv_symbols + coil_symbols + box_symbols

        # 6. Collect remaining conducting wire lines inside [x_left - 1.5, x_right + 1.5]
        #    Filter out full-width horizontal table row borders (width > 0.92 * (x_right - x_left))
        rail_span_w = x_right - x_left
        raw_wires: List[RawVectorLine] = []
        for ln in prims.lines:
            if ln.line_id in used_line_ids:
                continue
            b = ln.bbox
            if (
                b.x0 >= x_left - 1.5
                and b.x1 <= x_right + 1.5
                and b.y0 >= y_rail_min - 5.0
                and b.y1 <= y_rail_max + 5.0
            ):
                if abs(ln.y1 - ln.y0) <= 0.6 and b.width >= 0.92 * rail_span_w:
                    continue
                if math.hypot(ln.x1 - ln.x0, ln.y1 - ln.y0) >= 1.5:
                    raw_wires.append(ln)

        wire_lines = self._split_wires_at_inv_symbols(raw_wires, inv_symbols)

        # 7. Partition symbols and wires into electrically connected Rung Y-bands
        rung_groups = self._partition_rungs_by_electrical_y(wire_lines, all_symbols)

        rungs: List[LadderRungIR] = []
        for r_idx, (r_wires, r_syms, y_min, y_max) in enumerate(rung_groups):
            rung_ir = self._build_single_rung_graph(
                r_idx=r_idx,
                y_min=y_min,
                y_max=y_max,
                x_left=x_left,
                x_right=x_right,
                wires=r_wires,
                symbols=r_syms,
                prims=prims,
                profile=profile,
            )
            rungs.append(rung_ir)

        return rungs

    @staticmethod
    def _find_power_rails(
        lines: List[RawVectorLine],
        rects: List[RawVectorRect],
        page_height: float,
    ) -> Tuple[float, float, float, float, Set[str]]:
        """
        Finds the Left and Right Ladder Power Rails by grouping vertical segments (height >= 25pt)
        by their X coordinate (rounded to 0.2pt) and summing their total vertical span.
        In GX Works3 exports, the Left Power Rail (x=122.93) and Right Power Rail (x=770.99)
        are drawn as per-row vertical segments (38.64pt and 56.52pt each) whose combined vertical span
        exceeds 200pt. Outer table frame borders (e.g. x=68.5, x=84.1) lie to the left of x=122.93,
        so we select the innermost (largest X < 200pt) rail on the left and the outermost (largest X > 600pt)
        rail on the right.
        """
        x_bins: Dict[float, List[RawVectorLine]] = {}
        for ln in lines:
            if abs(ln.x1 - ln.x0) <= 0.8 and ln.bbox.height >= 25.0:
                xm = 0.5 * (ln.x0 + ln.x1)
                bin_key = round(xm * 5.0) / 5.0  # 0.2pt bin
                x_bins.setdefault(bin_key, []).append(ln)

        rail_groups: List[Tuple[float, float, float, float, List[str]]] = []
        for bin_key, segs in x_bins.items():
            total_h = sum(s.bbox.height for s in segs)
            if total_h >= page_height * 0.22:
                avg_x = sum(0.5 * (s.x0 + s.x1) for s in segs) / len(segs)
                min_y = min(s.bbox.y0 for s in segs)
                max_y = max(s.bbox.y1 for s in segs)
                rail_groups.append(
                    (avg_x, total_h, min_y, max_y, [s.line_id for s in segs])
                )

        if len(rail_groups) < 2:
            return (70.0, 760.0, 50.0, page_height - 30.0, set())

        left_candidates = [g for g in rail_groups if g[0] < 200.0]
        right_candidates = [g for g in rail_groups if g[0] > 600.0]

        left_rail = (
            max(left_candidates, key=lambda g: g[0])
            if left_candidates
            else min(rail_groups, key=lambda g: g[0])
        )
        right_rail = (
            max(right_candidates, key=lambda g: g[0])
            if right_candidates
            else max(rail_groups, key=lambda g: g[0])
        )

        x_left = left_rail[0]
        x_right = right_rail[0]
        y_min = min(left_rail[2], right_rail[2])
        y_max = max(left_rail[3], right_rail[3])

        rail_ids: Set[str] = set(left_rail[4]) | set(right_rail[4])
        for ln in lines:
            if abs(ln.x1 - ln.x0) <= 0.8:
                xm = 0.5 * (ln.x0 + ln.x1)
                if xm <= x_left + 0.8 or xm >= x_right - 0.8:
                    rail_ids.add(ln.line_id)

        return (x_left, x_right, y_min, y_max, rail_ids)

    def _detect_box_instructions(
        self,
        prims: PagePrimitives,
        x_left: float,
        x_right: float,
        y_rail_min: float,
        y_rail_max: float,
        used_text_ids: Set[str],
    ) -> Tuple[List[LadderSymbolNode], Set[str], Set[str]]:
        symbols: List[LadderSymbolNode] = []
        used_rects: Set[str] = set()
        used_lines: Set[str] = set()
        box_idx = 0

        horiz_lines = [
            ln
            for ln in prims.lines
            if abs(ln.y1 - ln.y0) <= 0.8 and ln.bbox.width >= 15.0
        ]
        all_horiz = [
            ln for ln in prims.lines if abs(ln.y1 - ln.y0) <= 0.8 and ln.bbox.width >= 1.5
        ]

        for r in prims.rects:
            b = r.bbox
            if b.x0 < x_left - 1.0 or b.x1 > x_right + 1.0:
                continue
            if b.y0 < y_rail_min - 10.0 or b.y1 > y_rail_max + 10.0:
                continue

            if r.is_filled and 20.0 <= b.width <= 33.0 and 14.0 <= b.height <= 48.0:
                hdr_spans = [
                    t
                    for t in prims.texts
                    if t.text_id not in used_text_ids
                    and b.x0 - 2.0 <= t.bbox.center[0] <= b.x1 + 2.0
                    and b.y0 - 2.0 <= t.bbox.center[1] <= b.y1 + 2.0
                ]
                if not hdr_spans:
                    continue
                hdr_spans.sort(key=lambda t: (t.bbox.y0, t.bbox.x0))
                mnemonic = hdr_spans[0].text.strip().upper()
                for t in hdr_spans:
                    used_text_ids.add(t.text_id)
                used_rects.add(r.rect_id)

                box_x1 = b.x1
                changed = True
                while changed:
                    changed = False
                    for hln in horiz_lines:
                        hb = hln.bbox
                        if abs(hb.y0 - b.y0) <= 1.2 or abs(hb.y0 - b.y1) <= 1.2:
                            if hb.x0 <= box_x1 + 1.5 and hb.x1 > box_x1 + 1.0:
                                box_x1 = hb.x1
                                changed = True

                full_bbox = BoundingBox(x0=b.x0, y0=b.y0, x1=box_x1, y1=b.y1)

                for ln in prims.lines:
                    lb = ln.bbox
                    if (
                        lb.x0 >= full_bbox.x0 - 1.2
                        and lb.x1 <= full_bbox.x1 + 1.2
                        and lb.y0 >= full_bbox.y0 - 1.2
                        and lb.y1 <= full_bbox.y1 + 1.2
                    ):
                        used_lines.add(ln.line_id)

                op_spans: List[RawTextSpan] = []
                comment_parts: List[str] = []
                for t in prims.texts:
                    if t.text_id in used_text_ids:
                        continue
                    tcx, tcy = t.bbox.center
                    if b.x1 - 1.0 <= tcx <= box_x1 + 2.0 and b.y0 - 2.0 <= tcy <= b.y1 + 2.0:
                        used_text_ids.add(t.text_id)
                        if t.bbox.y0 <= b.y0 + 9.5:
                            op_spans.append(t)
                        else:
                            comment_parts.append(t.text)

                op_spans.sort(key=lambda t: t.bbox.x0)
                raw_ops = [t.text.strip() for t in op_spans if t.text.strip()]
                parsed_ops = [self.lexer.parse(tok) for tok in raw_ops]
                target_dev = (
                    parsed_ops[0]
                    if len(parsed_ops) == 1
                    else (parsed_ops[-1] if parsed_ops else None)
                )
                if mnemonic == "OUT" and len(parsed_ops) >= 2:
                    target_dev = parsed_ops[0]

                is_compare = mnemonic in _COMPARE_MNEMONICS or (box_x1 < x_right - 55.0)
                if is_compare:
                    sym_kind = "COMPARE_BOX"
                elif mnemonic == "SET":
                    sym_kind = "SET_COIL"
                elif mnemonic == "RST":
                    sym_kind = "RST_COIL"
                elif mnemonic == "OUT" and len(parsed_ops) == 1:
                    sym_kind = "OUT_COIL"
                else:
                    sym_kind = "FUNC_BLOCK"

                port_y = b.y0 + 11.2
                for hw in all_horiz:
                    if hw.line_id in used_lines:
                        continue
                    hy = 0.5 * (hw.y0 + hw.y1)
                    if full_bbox.y0 + 2.0 <= hy <= full_bbox.y1 - 2.0:
                        if (
                            abs(hw.bbox.x1 - full_bbox.x0) <= 2.0
                            or abs(hw.bbox.x0 - full_bbox.x1) <= 2.0
                        ):
                            port_y = hy
                            break

                box_idx += 1
                symbols.append(
                    LadderSymbolNode(
                        symbol_id=f"SYM_BOX_{box_idx}",
                        symbol_kind=sym_kind,  # type: ignore[arg-type]
                        mnemonic=mnemonic,
                        device=target_dev,
                        operands=parsed_ops,
                        comment_text="".join(comment_parts) if comment_parts else None,
                        left_port_coord=(full_bbox.x0, port_y),
                        right_port_coord=(full_bbox.x1, port_y),
                        anchor=EvidenceAnchor(
                            doc_sha256=prims.doc_sha256,
                            page_no=prims.page_no,
                            bbox=full_bbox,
                            source_modality=SourceModality.VECTOR,
                            confidence=1.0,
                            raw_primitive_ids=[r.rect_id]
                            + [t.text_id for t in hdr_spans + op_spans],
                        ),
                    )
                )

            elif not r.is_filled and 35.0 <= b.width <= 180.0 and 12.0 <= b.height <= 45.0:
                inside_spans = [
                    t
                    for t in prims.texts
                    if t.text_id not in used_text_ids
                    and b.x0 - 2.0 <= t.bbox.center[0] <= b.x1 + 2.0
                    and b.y0 - 2.0 <= t.bbox.center[1] <= b.y1 + 2.0
                ]
                if not inside_spans:
                    continue
                inside_spans.sort(key=lambda t: t.bbox.x0)
                for t in inside_spans:
                    used_text_ids.add(t.text_id)
                used_rects.add(r.rect_id)

                for ln in prims.lines:
                    lb = ln.bbox
                    if (
                        lb.x0 >= b.x0 - 1.0
                        and lb.x1 <= b.x1 + 1.0
                        and lb.y0 >= b.y0 - 1.0
                        and lb.y1 <= b.y1 + 1.0
                    ):
                        used_lines.add(ln.line_id)

                full_text = " ".join(t.text for t in inside_spans).strip()
                tokens = full_text.split()
                mnemonic = tokens[0].upper() if tokens else "FUNC"
                raw_ops = tokens[1:] if len(tokens) > 1 else []
                parsed_ops = [self.lexer.parse(tok) for tok in raw_ops]
                target_dev = (
                    parsed_ops[0]
                    if len(parsed_ops) == 1
                    else (parsed_ops[-1] if parsed_ops else None)
                )

                if mnemonic in _COMPARE_MNEMONICS:
                    sym_kind = "COMPARE_BOX"
                elif mnemonic == "SET":
                    sym_kind = "SET_COIL"
                elif mnemonic == "RST":
                    sym_kind = "RST_COIL"
                else:
                    sym_kind = "FUNC_BLOCK"

                cy = b.center[1]
                box_idx += 1
                symbols.append(
                    LadderSymbolNode(
                        symbol_id=f"SYM_BOX_{box_idx}",
                        symbol_kind=sym_kind,  # type: ignore[arg-type]
                        mnemonic=mnemonic,
                        device=target_dev,
                        operands=parsed_ops,
                        comment_text=None,
                        left_port_coord=(b.x0, cy),
                        right_port_coord=(b.x1, cy),
                        anchor=EvidenceAnchor(
                            doc_sha256=prims.doc_sha256,
                            page_no=prims.page_no,
                            bbox=b,
                            source_modality=SourceModality.VECTOR,
                            confidence=1.0,
                            raw_primitive_ids=[r.rect_id]
                            + [t.text_id for t in inside_spans],
                        ),
                    )
                )

        return (symbols, used_rects, used_lines)

    def _detect_coils(
        self,
        prims: PagePrimitives,
        x_left: float,
        x_right: float,
        y_rail_min: float,
        y_rail_max: float,
        used_text_ids: Set[str],
    ) -> List[LadderSymbolNode]:
        symbols: List[LadderSymbolNode] = []
        used_curves: Set[str] = set()
        coil_idx = 0

        for c in prims.curves:
            if c.is_filled or c.curve_id in used_curves:
                continue
            b = c.bbox
            if (
                x_right - 45.0 <= b.x0 < x_right
                and y_rail_min <= b.center[1] <= y_rail_max
                and 4.2 <= b.width <= 12.0
                and 4.2 <= b.height <= 12.0
            ):
                used_curves.add(c.curve_id)
                coil_idx += 1
                dev_spans, dev_text = self._find_device_above(b, prims.texts, used_text_ids)
                dev_ir = self.lexer.parse(dev_text) if dev_text else None
                cy = b.center[1]
                prim_ids = [c.curve_id] + [s.text_id for s in dev_spans]
                symbols.append(
                    LadderSymbolNode(
                        symbol_id=f"SYM_COIL_{coil_idx}",
                        symbol_kind="OUT_COIL",
                        mnemonic="OUT",
                        device=dev_ir,
                        operands=[dev_ir] if dev_ir else [],
                        comment_text=None,
                        left_port_coord=(b.x0, cy),
                        right_port_coord=(b.x1, cy),
                        anchor=EvidenceAnchor(
                            doc_sha256=prims.doc_sha256,
                            page_no=prims.page_no,
                            bbox=b,
                            source_modality=SourceModality.VECTOR,
                            confidence=1.0,
                            raw_primitive_ids=prim_ids,
                        ),
                    )
                )

        arc_curves = [
            c
            for c in prims.curves
            if not c.is_filled
            and c.curve_id not in used_curves
            and x_left < c.bbox.x0
            and c.bbox.x1 < x_right
            and 8.0 <= c.bbox.height <= 24.0
        ]
        arc_curves.sort(key=lambda c: (round(c.bbox.center[1], 1), c.bbox.x0))

        for i, c1 in enumerate(arc_curves):
            if c1.curve_id in used_curves:
                continue
            for j in range(i + 1, len(arc_curves)):
                c2 = arc_curves[j]
                if c2.curve_id in used_curves:
                    continue
                if abs(c1.bbox.center[1] - c2.bbox.center[1]) <= 2.0:
                    dx = c2.bbox.center[0] - c1.bbox.center[0]
                    if 6.0 <= dx <= 22.0:
                        used_curves.add(c1.curve_id)
                        used_curves.add(c2.curve_id)
                        coil_idx += 1
                        bbox = BoundingBox(
                            x0=min(c1.bbox.x0, c2.bbox.x0),
                            y0=min(c1.bbox.y0, c2.bbox.y0),
                            x1=max(c1.bbox.x1, c2.bbox.x1),
                            y1=max(c1.bbox.y1, c2.bbox.y1),
                        )
                        dev_spans, dev_text = self._find_device_above(
                            bbox, prims.texts, used_text_ids
                        )
                        dev_ir = self.lexer.parse(dev_text) if dev_text else None
                        cy = bbox.center[1]
                        prim_ids = [c1.curve_id, c2.curve_id] + [
                            s.text_id for s in dev_spans
                        ]
                        symbols.append(
                            LadderSymbolNode(
                                symbol_id=f"SYM_COIL_{coil_idx}",
                                symbol_kind="OUT_COIL",
                                mnemonic="OUT",
                                device=dev_ir,
                                operands=[dev_ir] if dev_ir else [],
                                comment_text=None,
                                left_port_coord=(bbox.x0, cy),
                                right_port_coord=(bbox.x1, cy),
                                anchor=EvidenceAnchor(
                                    doc_sha256=prims.doc_sha256,
                                    page_no=prims.page_no,
                                    bbox=bbox,
                                    source_modality=SourceModality.VECTOR,
                                    confidence=1.0,
                                    raw_primitive_ids=prim_ids,
                                ),
                            )
                        )
                        break

        return symbols

    def _detect_contacts(
        self,
        prims: PagePrimitives,
        profile: PageModalityProfile,
        x_left: float,
        x_right: float,
        y_rail_min: float,
        y_rail_max: float,
        used_line_ids: Set[str],
        used_text_ids: Set[str],
    ) -> Tuple[List[LadderSymbolNode], Set[str]]:
        w_contact = profile.characteristic_contact_width_pt

        arrow_polys = [
            c
            for c in prims.curves
            if c.is_filled and len(c.points) >= 3 and 3.0 <= c.bbox.width <= 9.0
        ]

        vert_bars: List[RawVectorLine] = []
        for ln in prims.lines:
            if ln.line_id in used_line_ids:
                continue
            dx = abs(ln.x1 - ln.x0)
            dy = abs(ln.y1 - ln.y0)
            if (
                dx <= 0.6
                and 4.2 <= dy <= 24.0
                and x_left + 4.0 < ln.x0 < x_right - 4.0
                and y_rail_min - 5.0 <= ln.bbox.center[1] <= y_rail_max + 5.0
            ):
                vert_bars.append(ln)

        vert_bars.sort(key=lambda ln: (round(ln.bbox.center[1], 1), ln.bbox.center[0]))
        paired_ids: Set[str] = set()
        contact_lines: Set[str] = set()
        symbols: List[LadderSymbolNode] = []

        c_idx = 0
        for i, b1 in enumerate(vert_bars):
            if b1.line_id in paired_ids:
                continue
            x1, y1 = b1.bbox.center
            h1 = b1.bbox.height
            for j in range(i + 1, len(vert_bars)):
                b2 = vert_bars[j]
                if b2.line_id in paired_ids:
                    continue
                x2, y2 = b2.bbox.center
                h2 = b2.bbox.height
                if abs(y2 - y1) <= 1.5 and abs(h2 - h1) <= 2.0:
                    dist_x = x2 - x1
                    if 0.75 * w_contact <= dist_x <= 1.35 * w_contact:
                        paired_ids.add(b1.line_id)
                        paired_ids.add(b2.line_id)
                        contact_lines.add(b1.line_id)
                        contact_lines.add(b2.line_id)
                        c_idx += 1

                        bbox = BoundingBox(
                            x0=min(b1.bbox.x0, b2.bbox.x0),
                            y0=min(b1.bbox.y0, b2.bbox.y0),
                            x1=max(b1.bbox.x1, b2.bbox.x1),
                            y1=max(b1.bbox.y1, b2.bbox.y1),
                        )

                        interior_ids: List[str] = []
                        has_slash = False
                        arrow_top_angled = 0
                        arrow_bot_angled = 0
                        for ln in prims.lines:
                            if ln.line_id in contact_lines or ln.line_id in used_line_ids:
                                continue
                            lb = ln.bbox
                            if (
                                lb.x0 >= bbox.x0 - 2.5
                                and lb.x1 <= bbox.x1 + 2.5
                                and lb.y0 >= bbox.y0 - 2.5
                                and lb.y1 <= bbox.y1 + 2.5
                            ):
                                interior_ids.append(ln.line_id)
                                paired_ids.add(ln.line_id)
                                dx_in = abs(ln.x1 - ln.x0)
                                dy_in = abs(ln.y1 - ln.y0)
                                if dx_in >= 0.7 * dist_x and dy_in >= 0.5 * bbox.height:
                                    has_slash = True
                                elif 1.0 <= dx_in <= 3.5 and 1.0 <= dy_in <= 5.0:
                                    if lb.center[1] < bbox.center[1]:
                                        arrow_top_angled += 1
                                    else:
                                        arrow_bot_angled += 1

                        poly_ids: List[str] = []
                        for ap in arrow_polys:
                            ab = ap.bbox
                            if (
                                ab.x0 >= bbox.x0 - 1.5
                                and ab.x1 <= bbox.x1 + 1.5
                                and ab.y0 >= bbox.y0 - 1.5
                                and ab.y1 <= bbox.y1 + 1.5
                            ):
                                poly_ids.append(ap.curve_id)
                                if ab.center[1] <= bbox.center[1] + 0.2:
                                    arrow_top_angled += 2
                                else:
                                    arrow_bot_angled += 2

                        contact_lines.update(interior_ids)
                        if has_slash:
                            sym_kind = "NC_CONTACT"
                            mnemonic = "ANI"
                        elif arrow_top_angled >= 2:
                            sym_kind = "RISE_CONTACT"
                            mnemonic = "ANDP"
                        elif arrow_bot_angled >= 2:
                            sym_kind = "FALL_CONTACT"
                            mnemonic = "ANDF"
                        else:
                            sym_kind = "NO_CONTACT"
                            mnemonic = "AND"

                        dev_spans, dev_text = self._find_device_above(
                            bbox, prims.texts, used_text_ids
                        )
                        dev_ir = self.lexer.parse(dev_text) if dev_text else None
                        cy = bbox.center[1]
                        prim_ids = (
                            [b1.line_id, b2.line_id]
                            + interior_ids
                            + poly_ids
                            + [s.text_id for s in dev_spans]
                        )

                        symbols.append(
                            LadderSymbolNode(
                                symbol_id=f"SYM_CONT_{c_idx}",
                                symbol_kind=sym_kind,  # type: ignore[arg-type]
                                mnemonic=mnemonic,
                                device=dev_ir,
                                operands=[dev_ir] if dev_ir else [],
                                comment_text=None,
                                left_port_coord=(bbox.x0, cy),
                                right_port_coord=(bbox.x1, cy),
                                anchor=EvidenceAnchor(
                                    doc_sha256=prims.doc_sha256,
                                    page_no=prims.page_no,
                                    bbox=bbox,
                                    source_modality=SourceModality.VECTOR,
                                    confidence=1.0,
                                    raw_primitive_ids=prim_ids,
                                ),
                            )
                        )
                        break

        for ln in vert_bars:
            if ln.line_id in paired_ids:
                continue
            lb = ln.bbox
            for ap in arrow_polys:
                ab = ap.bbox
                if (
                    ab.x0 - 1.5 <= lb.center[0] <= ab.x1 + 1.5
                    and ab.y0 - 3.0 <= lb.center[1] <= ab.y1 + 3.0
                ):
                    contact_lines.add(ln.line_id)
                    break

        return (symbols, contact_lines)

    def _detect_inv_slashes(
        self,
        prims: PagePrimitives,
        x_left: float,
        x_right: float,
        y_rail_min: float,
        y_rail_max: float,
        used_line_ids: Set[str],
    ) -> Tuple[List[LadderSymbolNode], Set[str]]:
        inv_symbols: List[LadderSymbolNode] = []
        inv_lines: Set[str] = set()
        inv_idx = 0
        for ln in prims.lines:
            if ln.line_id in used_line_ids:
                continue
            dx = abs(ln.x1 - ln.x0)
            dy = abs(ln.y1 - ln.y0)
            b = ln.bbox
            if (
                5.0 <= dx <= 12.0
                and 4.0 <= dy <= 12.0
                and x_left + 5.0 < b.x0 < x_right - 5.0
                and y_rail_min <= b.center[1] <= y_rail_max
            ):
                inv_idx += 1
                inv_lines.add(ln.line_id)
                cy = b.center[1]
                inv_symbols.append(
                    LadderSymbolNode(
                        symbol_id=f"SYM_INV_{inv_idx}",
                        symbol_kind="INV_LINE",
                        mnemonic="INV",
                        device=None,
                        operands=[],
                        comment_text=None,
                        left_port_coord=(b.x0, cy),
                        right_port_coord=(b.x1, cy),
                        anchor=EvidenceAnchor(
                            doc_sha256=prims.doc_sha256,
                            page_no=prims.page_no,
                            bbox=b,
                            source_modality=SourceModality.VECTOR,
                            confidence=1.0,
                            raw_primitive_ids=[ln.line_id],
                        ),
                    )
                )
        return (inv_symbols, inv_lines)

    @staticmethod
    def _split_wires_at_inv_symbols(
        wires: List[RawVectorLine], inv_symbols: List[LadderSymbolNode]
    ) -> List[RawVectorLine]:
        if not inv_symbols:
            return wires
        result: List[RawVectorLine] = []
        for w in wires:
            if abs(w.y1 - w.y0) > 0.6:
                result.append(w)
                continue
            wy = 0.5 * (w.y0 + w.y1)
            wx0, wx1 = min(w.x0, w.x1), max(w.x0, w.x1)
            split_done = False
            for inv in inv_symbols:
                ib = inv.anchor.bbox
                if abs(wy - ib.center[1]) <= 1.5 and wx0 < ib.x0 and wx1 > ib.x1:
                    result.append(
                        RawVectorLine(
                            line_id=f"{w.line_id}_L",
                            page_no=w.page_no,
                            x0=wx0,
                            y0=wy,
                            x1=ib.x0,
                            y1=wy,
                            stroke_width=w.stroke_width,
                        )
                    )
                    result.append(
                        RawVectorLine(
                            line_id=f"{w.line_id}_R",
                            page_no=w.page_no,
                            x0=ib.x1,
                            y0=wy,
                            x1=wx1,
                            y1=wy,
                            stroke_width=w.stroke_width,
                        )
                    )
                    split_done = True
                    break
            if not split_done:
                result.append(w)
        return result

    @staticmethod
    def _find_device_above(
        sym_bbox: BoundingBox, texts: List[RawTextSpan], used_text_ids: Set[str]
    ) -> Tuple[List[RawTextSpan], Optional[str]]:
        cx = sym_bbox.center[0]
        candidates: List[RawTextSpan] = []
        for t in texts:
            if t.text_id in used_text_ids:
                continue
            tcx, tcy = t.bbox.center
            if sym_bbox.y0 - 20.0 <= tcy <= sym_bbox.y0 + 1.5 and abs(tcx - cx) <= 20.0:
                candidates.append(t)
        if not candidates:
            return ([], None)
        candidates.sort(key=lambda t: t.bbox.y0)
        closest = min(
            candidates,
            key=lambda t: math.hypot(t.bbox.center[0] - cx, sym_bbox.y0 - t.bbox.center[1]),
        )
        if closest.text == "…" and len(candidates) >= 2:
            for c in candidates:
                used_text_ids.add(c.text_id)
            return (candidates, "".join(c.text for c in candidates))
        used_text_ids.add(closest.text_id)
        return ([closest], closest.text)

    @staticmethod
    def _find_comment_below(
        sym_bbox: BoundingBox, texts: List[RawTextSpan], used_text_ids: Set[str]
    ) -> Optional[str]:
        cx = sym_bbox.center[0]
        col_spans: List[RawTextSpan] = []
        for t in texts:
            if t.text_id in used_text_ids:
                continue
            tcx, tcy = t.bbox.center
            if sym_bbox.y1 - 1.0 <= t.bbox.y0 <= sym_bbox.y1 + 29.0 and abs(tcx - cx) <= 25.0:
                col_spans.append(t)
        if not col_spans:
            return None
        col_spans.sort(key=lambda t: (t.bbox.y0, t.bbox.x0))
        for t in col_spans:
            used_text_ids.add(t.text_id)
        return "".join(t.text for t in col_spans)

    @staticmethod
    def _partition_rungs_by_electrical_y(
        wires: List[RawVectorLine],
        symbols: List[LadderSymbolNode],
    ) -> List[Tuple[List[RawVectorLine], List[LadderSymbolNode], float, float]]:
        intervals: List[Tuple[float, float]] = []
        for w in wires:
            b = w.bbox
            intervals.append((b.y0 - 2.5, b.y1 + 2.5))
        for s in symbols:
            py = s.left_port_coord[1]
            intervals.append((py - 4.0, py + 4.0))

        if not intervals:
            return []
        intervals.sort(key=lambda iv: iv[0])
        merged_elec: List[Tuple[float, float]] = [intervals[0]]
        for y0, y1 in intervals[1:]:
            prev_y0, prev_y1 = merged_elec[-1]
            if y0 <= prev_y1 + 3.0:
                merged_elec[-1] = (prev_y0, max(prev_y1, y1))
            else:
                merged_elec.append((y0, y1))

        groups: List[Tuple[List[RawVectorLine], List[LadderSymbolNode], float, float]] = []
        for ey_min, ey_max in merged_elec:
            r_wires = [
                w
                for w in wires
                if w.bbox.center[1] >= ey_min - 2.0 and w.bbox.center[1] <= ey_max + 2.0
            ]
            r_syms = [
                s
                for s in symbols
                if s.left_port_coord[1] >= ey_min - 3.0 and s.left_port_coord[1] <= ey_max + 3.0
            ]
            if not r_syms:
                continue
            vis_y_min = min(
                [ey_min]
                + [w.bbox.y0 for w in r_wires]
                + [s.anchor.bbox.y0 - 8.0 for s in r_syms]
            )
            vis_y_max = max(
                [ey_max]
                + [w.bbox.y1 for w in r_wires]
                + [s.anchor.bbox.y1 + 8.0 for s in r_syms]
            )
            groups.append((r_wires, r_syms, vis_y_min, vis_y_max))

        return groups

    def _build_single_rung_graph(
        self,
        r_idx: int,
        y_min: float,
        y_max: float,
        x_left: float,
        x_right: float,
        wires: List[RawVectorLine],
        symbols: List[LadderSymbolNode],
        prims: PagePrimitives,
        profile: PageModalityProfile,
    ) -> LadderRungIR:
        eps_snap = profile.tolerance_snap_pt
        eps_collinear = profile.tolerance_collinear_pt
        eps_guard = profile.tolerance_symbol_guard_pt

        geoms: List[LineString] = [
            LineString([(w.x0, w.y0), (w.x1, w.y1)]) for w in wires
        ]
        dsu = DisjointSet(len(wires))
        merge_logs: List[GeometryMergeRecord] = []
        merge_seq = 0

        if geoms:
            tree = STRtree(geoms)
            for i, g_i in enumerate(geoms):
                w_i = wires[i]
                buffered = g_i.buffer(eps_snap)
                cand_indices = tree.query(buffered)
                for j in cand_indices:
                    j_int = int(j)
                    if j_int <= i:
                        continue
                    g_j = geoms[j_int]
                    w_j = wires[j_int]
                    dist = float(g_i.distance(g_j))
                    if dist <= eps_snap and dist < eps_guard:
                        pts_i = [(w_i.x0, w_i.y0), (w_i.x1, w_i.y1)]
                        pts_j = [(w_j.x0, w_j.y0), (w_j.x1, w_j.y1)]
                        best_pair = (pts_i[0], pts_j[0])
                        min_pt_d = 999.0
                        for pa in pts_i:
                            for pb in pts_j:
                                d_ab = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                                if d_ab < min_pt_d:
                                    min_pt_d = d_ab
                                    best_pair = (pa, pb)

                        is_collinear_h = (
                            abs(w_i.y1 - w_i.y0) <= 0.5
                            and abs(w_j.y1 - w_j.y0) <= 0.5
                            and abs(w_i.y0 - w_j.y0) <= 0.5
                        )
                        rule_name = (
                            "collinear_stitch"
                            if (is_collinear_h and min_pt_d <= eps_collinear)
                            else ("endpoint_snap" if min_pt_d <= eps_snap else "t_junction_snap")
                        )

                        merged_new = dsu.union(i, j_int)
                        if merged_new and dist > 1e-4:
                            merge_seq += 1
                            merge_logs.append(
                                GeometryMergeRecord(
                                    merge_id=f"M_r{r_idx}_{merge_seq}",
                                    page_no=prims.page_no,
                                    primitive_a_id=w_i.line_id,
                                    primitive_b_id=w_j.line_id,
                                    point_a=best_pair[0],
                                    point_b=best_pair[1],
                                    euclidean_distance_pt=round(dist, 4),
                                    applied_tolerance_pt=eps_snap,
                                    rule_name=rule_name,  # type: ignore[arg-type]
                                )
                            )

        clusters: Dict[int, List[int]] = {}
        for i in range(len(wires)):
            root = dsu.find(i)
            clusters.setdefault(root, []).append(i)

        root_to_jid: Dict[int, str] = {}
        junction_coords: Dict[str, Tuple[float, float]] = {
            "J_LEFT": (x_left, 0.5 * (y_min + y_max)),
            "J_RIGHT": (x_right, 0.5 * (y_min + y_max)),
        }
        j_counter = 0

        sorted_roots = sorted(
            clusters.keys(),
            key=lambda r: min(wires[idx].bbox.x0 for idx in clusters[r]),
        )
        for root in sorted_roots:
            idxs = clusters[root]
            min_x = min(wires[idx].bbox.x0 for idx in idxs)
            max_x = max(wires[idx].bbox.x1 for idx in idxs)
            avg_x = sum(wires[idx].bbox.center[0] for idx in idxs) / len(idxs)
            avg_y = sum(wires[idx].bbox.center[1] for idx in idxs) / len(idxs)

            if abs(min_x - x_left) <= eps_snap + 1.8:
                root_to_jid[root] = "J_LEFT"
            elif abs(max_x - x_right) <= eps_snap + 1.8:
                root_to_jid[root] = "J_RIGHT"
            else:
                j_counter += 1
                jid = f"J_{r_idx}_{j_counter}"
                root_to_jid[root] = jid
                junction_coords[jid] = (round(avg_x, 2), round(avg_y, 2))

        edges: List[GeometryEdge] = []
        symbols_sorted = sorted(
            symbols,
            key=lambda s: (round(s.left_port_coord[1], 1), s.anchor.bbox.x0),
        )
        for e_idx, sym in enumerate(symbols_sorted, start=1):
            src_j = self._attach_port_to_junction(
                sym.left_port_coord,
                geoms,
                dsu,
                root_to_jid,
                x_left,
                x_right,
                eps_snap + 3.0,
            )
            dst_j = self._attach_port_to_junction(
                sym.right_port_coord,
                geoms,
                dsu,
                root_to_jid,
                x_left,
                x_right,
                eps_snap + 3.0,
            )
            if dst_j == "J_UNCONNECTED" and sym.symbol_kind in _TERMINAL_OUTPUT_KINDS:
                dst_j = "J_RIGHT"

            edges.append(
                GeometryEdge(
                    edge_id=f"E_{r_idx}_{e_idx}",
                    source_node=src_j,
                    target_node=dst_j,
                    symbol=sym,
                )
            )

        rung_bbox = BoundingBox(x0=x_left, y0=y_min, x1=x_right, y1=y_max)
        return LadderRungIR(
            rung_id=f"RUNG_{r_idx}",
            rung_index=r_idx,
            junction_coords=junction_coords,
            left_rail_junction="J_LEFT",
            right_rail_junction="J_RIGHT",
            geometry_nodes=symbols_sorted,
            geometry_edges=edges,
            merge_audit_logs=merge_logs,
            anchor=EvidenceAnchor(
                doc_sha256=prims.doc_sha256,
                page_no=prims.page_no,
                bbox=rung_bbox,
                source_modality=SourceModality.VECTOR,
                confidence=1.0,
                raw_primitive_ids=[w.line_id for w in wires],
            ),
        )

    @staticmethod
    def _attach_port_to_junction(
        port_xy: Tuple[float, float],
        geoms: List[LineString],
        dsu: DisjointSet,
        root_to_jid: Dict[int, str],
        x_left: float,
        x_right: float,
        max_tol: float,
    ) -> str:
        pt = Point(port_xy[0], port_xy[1])
        best_dist = 999.0
        best_root: Optional[int] = None
        for i, g in enumerate(geoms):
            d = float(g.distance(pt))
            if d < best_dist:
                best_dist = d
                best_root = dsu.find(i)
        if best_root is not None and best_dist <= max_tol:
            return root_to_jid[best_root]
        if abs(port_xy[0] - x_left) <= max_tol:
            return "J_LEFT"
        if abs(port_xy[0] - x_right) <= max_tol:
            return "J_RIGHT"
        return "J_UNCONNECTED"
