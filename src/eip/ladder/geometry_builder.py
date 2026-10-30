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
    P0-03: Deterministic Vector Ladder Geometry & Topology Builder.
    - Identifies Left/Right Power Rails and Rung horizontal bands
    - Recognizes NO/NC/RISE/FALL contacts, output coils, and rectangular box instructions
    - Binds device addresses via FX5UDeviceLexer and comments via spatial proximity
    - Uses Shapely STRtree + Adaptive Tolerance (eps_snap, eps_collinear, eps_guard)
      to stitch micro-gaps, cluster wire junctions, and build the directed Geometry Graph
    - Emits full GeometryMergeRecord audit trails for every non-zero geometric snap
    """

    def __init__(self, lexer: Optional[FX5UDeviceLexer] = None) -> None:
        self.lexer = lexer or FX5UDeviceLexer()

    def build_page_rungs(
        self, prims: PagePrimitives, profile: PageModalityProfile
    ) -> List[LadderRungIR]:
        x_left, x_right, rail_ids = self._find_power_rails(prims.lines, prims.height)
        used_line_ids: Set[str] = set(rail_ids)
        used_text_ids: Set[str] = set()

        # 1. Detect all symbols across the page
        box_symbols, box_rect_ids, box_line_ids = self._detect_box_instructions(
            prims, x_left, x_right, used_text_ids
        )
        used_line_ids.update(box_line_ids)

        coil_symbols = self._detect_coils(prims, x_left, x_right, used_text_ids)

        contact_symbols, contact_line_ids = self._detect_contacts(
            prims, profile, x_left, x_right, used_line_ids, used_text_ids
        )
        used_line_ids.update(contact_line_ids)

        all_symbols = contact_symbols + coil_symbols + box_symbols

        # 2. Remaining lines inside [x_left - 2, x_right + 2] are candidate conducting wires
        wire_lines: List[RawVectorLine] = []
        for ln in prims.lines:
            if ln.line_id in used_line_ids:
                continue
            b = ln.bbox
            if b.x0 >= x_left - 2.0 and b.x1 <= x_right + 2.0:
                wire_lines.append(ln)

        # 3. Partition symbols and wires into Rung Y-bands
        rung_bands = self._partition_rung_bands(wire_lines, all_symbols, x_left)

        rungs: List[LadderRungIR] = []
        for r_idx, (y_min, y_max) in enumerate(rung_bands):
            r_wires = [
                ln
                for ln in wire_lines
                if (ln.bbox.center[1] >= y_min - 5.0 and ln.bbox.center[1] <= y_max + 5.0)
            ]
            r_syms = [
                s
                for s in all_symbols
                if (s.anchor.bbox.center[1] >= y_min - 10.0 and s.anchor.bbox.center[1] <= y_max + 10.0)
            ]
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
        lines: List[RawVectorLine], page_height: float
    ) -> Tuple[float, float, Set[str]]:
        vert_rails: List[Tuple[float, float, str]] = []
        for ln in lines:
            if abs(ln.x1 - ln.x0) <= 0.8 and abs(ln.y1 - ln.y0) >= page_height * 0.45:
                vert_rails.append((0.5 * (ln.x0 + ln.x1), abs(ln.y1 - ln.y0), ln.line_id))
        if len(vert_rails) < 2:
            return (70.0, 760.0, set())
        vert_rails.sort(key=lambda item: item[0])
        left_rail = vert_rails[0]
        right_rail = vert_rails[-1]
        return (left_rail[0], right_rail[0], {left_rail[2], right_rail[2]})

    def _detect_box_instructions(
        self,
        prims: PagePrimitives,
        x_left: float,
        x_right: float,
        used_text_ids: Set[str],
    ) -> Tuple[List[LadderSymbolNode], Set[str], Set[str]]:
        symbols: List[LadderSymbolNode] = []
        used_rects: Set[str] = set()
        used_lines: Set[str] = set()

        for idx, r in enumerate(prims.rects, start=1):
            b = r.bbox
            if b.x0 < x_left or b.x1 > x_right:
                continue
            if not (35.0 <= b.width <= 180.0 and 12.0 <= b.height <= 45.0):
                continue

            # Find text spans inside the box
            inside_spans: List[RawTextSpan] = []
            for t in prims.texts:
                tcx, tcy = t.bbox.center
                if b.x0 - 2.0 <= tcx <= b.x1 + 2.0 and b.y0 - 2.0 <= tcy <= b.y1 + 2.0:
                    inside_spans.append(t)

            if not inside_spans:
                continue

            inside_spans.sort(key=lambda t: t.bbox.x0)
            for t in inside_spans:
                used_text_ids.add(t.text_id)
            used_rects.add(r.rect_id)

            # Also mark any lines that overlap the rectangle border as used
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
            target_dev = parsed_ops[0] if len(parsed_ops) == 1 else (parsed_ops[-1] if parsed_ops else None)

            if mnemonic == "SET":
                sym_kind = "SET_COIL"
            elif mnemonic == "RST":
                sym_kind = "RST_COIL"
            else:
                sym_kind = "FUNC_BLOCK"

            comment = self._find_comment_below(b, prims.texts, used_text_ids)
            cy = b.center[1]
            symbols.append(
                LadderSymbolNode(
                    symbol_id=f"SYM_BOX_{idx}",
                    symbol_kind=sym_kind,  # type: ignore[arg-type]
                    mnemonic=mnemonic,
                    device=target_dev,
                    operands=parsed_ops,
                    comment_text=comment,
                    left_port_coord=(b.x0, cy),
                    right_port_coord=(b.x1, cy),
                    anchor=EvidenceAnchor(
                        doc_sha256=prims.doc_sha256,
                        page_no=prims.page_no,
                        bbox=b,
                        source_modality=SourceModality.VECTOR,
                        confidence=1.0,
                        raw_primitive_ids=[r.rect_id] + [t.text_id for t in inside_spans],
                    ),
                )
            )

        return (symbols, used_rects, used_lines)

    def _detect_coils(
        self,
        prims: PagePrimitives,
        x_left: float,
        x_right: float,
        used_text_ids: Set[str],
    ) -> List[LadderSymbolNode]:
        symbols: List[LadderSymbolNode] = []
        curves = [
            c
            for c in prims.curves
            if x_left < c.bbox.x0 and c.bbox.x1 < x_right and 8.0 <= c.bbox.height <= 24.0
        ]
        curves.sort(key=lambda c: (round(c.bbox.center[1], 1), c.bbox.x0))
        used_curves: Set[str] = set()

        coil_idx = 0
        for i, c1 in enumerate(curves):
            if c1.curve_id in used_curves:
                continue
            for j in range(i + 1, len(curves)):
                c2 = curves[j]
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
                        dev_span = self._find_device_above(bbox, prims.texts, used_text_ids)
                        dev_ir = self.lexer.parse(dev_span.text) if dev_span else None
                        comment = self._find_comment_below(bbox, prims.texts, used_text_ids)
                        cy = bbox.center[1]
                        prim_ids = [c1.curve_id, c2.curve_id]
                        if dev_span:
                            prim_ids.append(dev_span.text_id)
                        symbols.append(
                            LadderSymbolNode(
                                symbol_id=f"SYM_COIL_{coil_idx}",
                                symbol_kind="OUT_COIL",
                                mnemonic="OUT",
                                device=dev_ir,
                                operands=[dev_ir] if dev_ir else [],
                                comment_text=comment,
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
        used_line_ids: Set[str],
        used_text_ids: Set[str],
    ) -> Tuple[List[LadderSymbolNode], Set[str]]:
        w_contact = profile.characteristic_contact_width_pt
        vert_bars: List[RawVectorLine] = []
        for ln in prims.lines:
            if ln.line_id in used_line_ids:
                continue
            dx = abs(ln.x1 - ln.x0)
            dy = abs(ln.y1 - ln.y0)
            if dx <= 0.6 and 8.0 <= dy <= 24.0 and x_left + 5.0 < ln.x0 < x_right - 5.0:
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
                    if 0.70 * w_contact <= dist_x <= 1.35 * w_contact:
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

                        # Inspect interior lines for NC diagonal slash or RISE/FALL arrow
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
                                dx_in = abs(ln.x1 - ln.x0)
                                dy_in = abs(ln.y1 - ln.y0)
                                if dx_in >= 0.7 * dist_x and dy_in >= 0.5 * bbox.height:
                                    has_slash = True
                                elif 1.0 <= dx_in <= 3.5 and 1.0 <= dy_in <= 5.0:
                                    if lb.center[1] < bbox.center[1]:
                                        arrow_top_angled += 1
                                    else:
                                        arrow_bot_angled += 1

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

                        dev_span = self._find_device_above(bbox, prims.texts, used_text_ids)
                        dev_ir = self.lexer.parse(dev_span.text) if dev_span else None
                        comment = self._find_comment_below(bbox, prims.texts, used_text_ids)
                        cy = bbox.center[1]
                        prim_ids = [b1.line_id, b2.line_id] + interior_ids
                        if dev_span:
                            prim_ids.append(dev_span.text_id)

                        symbols.append(
                            LadderSymbolNode(
                                symbol_id=f"SYM_CONT_{c_idx}",
                                symbol_kind=sym_kind,  # type: ignore[arg-type]
                                mnemonic=mnemonic,
                                device=dev_ir,
                                operands=[dev_ir] if dev_ir else [],
                                comment_text=comment,
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

        return (symbols, contact_lines)

    @staticmethod
    def _find_device_above(
        sym_bbox: BoundingBox, texts: List[RawTextSpan], used_text_ids: Set[str]
    ) -> Optional[RawTextSpan]:
        cx = sym_bbox.center[0]
        best_span: Optional[RawTextSpan] = None
        best_dist = 999.0
        for t in texts:
            if t.text_id in used_text_ids:
                continue
            tcx, tcy = t.bbox.center
            if sym_bbox.y0 - 20.0 <= tcy <= sym_bbox.y0 + 1.0 and abs(tcx - cx) <= 22.0:
                dist = math.hypot(tcx - cx, sym_bbox.y0 - tcy)
                if dist < best_dist:
                    best_dist = dist
                    best_span = t
        if best_span:
            used_text_ids.add(best_span.text_id)
        return best_span

    @staticmethod
    def _find_comment_below(
        sym_bbox: BoundingBox, texts: List[RawTextSpan], used_text_ids: Set[str]
    ) -> Optional[str]:
        cx = sym_bbox.center[0]
        best_span: Optional[RawTextSpan] = None
        best_dist = 999.0
        for t in texts:
            if t.text_id in used_text_ids:
                continue
            tcx, tcy = t.bbox.center
            if sym_bbox.y1 - 1.0 <= tcy <= sym_bbox.y1 + 20.0 and abs(tcx - cx) <= 35.0:
                dist = math.hypot(tcx - cx, tcy - sym_bbox.y1)
                if dist < best_dist:
                    best_dist = dist
                    best_span = t
        if best_span:
            used_text_ids.add(best_span.text_id)
            return best_span.text
        return None

    @staticmethod
    def _partition_rung_bands(
        wires: List[RawVectorLine],
        symbols: List[LadderSymbolNode],
        x_left: float,
    ) -> List[Tuple[float, float]]:
        """Clusters Y-intervals of wires and symbols into distinct Rung horizontal bands."""
        intervals: List[Tuple[float, float]] = []
        for ln in wires:
            b = ln.bbox
            intervals.append((b.y0 - 12.0, b.y1 + 12.0))
        for s in symbols:
            b = s.anchor.bbox
            intervals.append((b.y0 - 15.0, b.y1 + 15.0))
        if not intervals:
            return []
        intervals.sort(key=lambda iv: iv[0])
        merged: List[Tuple[float, float]] = [intervals[0]]
        for y0, y1 in intervals[1:]:
            prev_y0, prev_y1 = merged[-1]
            if y0 <= prev_y1 + 18.0:
                merged[-1] = (prev_y0, max(prev_y1, y1))
            else:
                merged.append((y0, y1))
        return merged

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

        # Build Shapely LineStrings and STRtree for wire segments
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
                # Query candidate nearby segments within eps_snap
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
                        # Determine closest points for audit record
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

        # Group wire segments by connected component root
        clusters: Dict[int, List[int]] = {}
        for i in range(len(wires)):
            root = dsu.find(i)
            clusters.setdefault(root, []).append(i)

        # Map each cluster root to a canonical Junction ID ('J_LEFT', 'J_RIGHT', or 'J_r_k')
        root_to_jid: Dict[int, str] = {}
        junction_coords: Dict[str, Tuple[float, float]] = {
            "J_LEFT": (x_left, 0.5 * (y_min + y_max)),
            "J_RIGHT": (x_right, 0.5 * (y_min + y_max)),
        }
        j_counter = 0

        # Sort clusters left-to-right for deterministic Junction naming
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

            if abs(min_x - x_left) <= eps_snap + 1.0:
                root_to_jid[root] = "J_LEFT"
            elif abs(max_x - x_right) <= eps_snap + 1.0:
                root_to_jid[root] = "J_RIGHT"
            else:
                j_counter += 1
                jid = f"J_{r_idx}_{j_counter}"
                root_to_jid[root] = jid
                junction_coords[jid] = (round(avg_x, 2), round(avg_y, 2))

        # Connect each symbol's left and right port to the closest wire cluster
        edges: List[GeometryEdge] = []
        symbols_sorted = sorted(
            symbols,
            key=lambda s: (round(s.anchor.bbox.center[1], 1), s.anchor.bbox.x0),
        )
        for e_idx, sym in enumerate(symbols_sorted, start=1):
            src_j = self._attach_port_to_junction(
                sym.left_port_coord,
                geoms,
                dsu,
                root_to_jid,
                x_left,
                x_right,
                eps_snap + 2.0,
            )
            dst_j = self._attach_port_to_junction(
                sym.right_port_coord,
                geoms,
                dsu,
                root_to_jid,
                x_left,
                x_right,
                eps_snap + 2.0,
            )
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
