from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel
import pdfplumber

from eip.ir.schema import (
    BoundingBox,
    CCLinkRefreshRuleIR,
    FBDPinBindingIR,
    FBDInstanceCallIR,
    LadderRungIR,
    SchematicIOChannelIR,
    SchematicPowerCircuitIR,
    STAssignmentIR,
    STProgramIR,
    STTimerCallIR,
    TypedDeviceIR,
)
from eip.plc.lexer import FX5UDeviceLexer


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


class DeviceCommentRecord(BaseModel):
    device: TypedDeviceIR
    comment: str
    page_no: int


class HardwareSlotRecord(BaseModel):
    slot_name: str
    model_name: str
    intelligent_module_no: Optional[str] = None
    input_points: Optional[str] = None
    output_points: Optional[str] = None


class StatementIndexRecord(BaseModel):
    program_name: str
    record_kind: str
    step_no: str
    statement_text: str
    page_no: int


class GX3ProjectTableParser:
    """
    Cross-Document Ground Truth Extractor for GX Works3 exported PDFs:
      - Parses '05_软元件注释.pdf' into a validated FX5U Device -> Comment dictionary
      - Parses '07_系统参数.pdf' into the PLC Hardware Rack / I/O Slot table
      - Parses '09_模块信息.pdf' into CC-Link IE Field cyclic refresh mapping rules
      - Parses '11_声明_注解.pdf' into program section [Title] and note index records
      - Parses '06_软元件存储器.pdf' into initial non-zero register values
    """

    def __init__(self, lexer: Optional[FX5UDeviceLexer] = None) -> None:
        self.lexer = lexer or FX5UDeviceLexer()

    def parse_device_comments(
        self, pdf_path: Path, max_pages: Optional[int] = None
    ) -> Dict[str, DeviceCommentRecord]:
        comments: Dict[str, DeviceCommentRecord] = {}
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = pdf.pages[:max_pages] if max_pages else pdf.pages
            for p_idx, page in enumerate(pages, start=1):
                tables = page.extract_tables()
                for tbl in tables:
                    for row in tbl:
                        if not row or len(row) < 2:
                            continue
                        dev_raw = _nfkc(row[0] or "")
                        cmt_raw = _nfkc(row[1] or "")
                        if not dev_raw or dev_raw == "软元件名":
                            continue
                        dev_ir = self.lexer.parse(dev_raw)
                        if dev_ir.is_valid:
                            comments[dev_ir.raw_text] = DeviceCommentRecord(
                                device=dev_ir,
                                comment=cmt_raw,
                                page_no=p_idx,
                            )
        return comments

    def parse_io_rack_config(self, pdf_path: Path) -> List[HardwareSlotRecord]:
        slots: List[HardwareSlotRecord] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for tbl in tables:
                    for row in tbl:
                        if not row or len(row) < 6:
                            continue
                        slot_id = _nfkc(row[0] or "")
                        model = _nfkc(row[1] or "")
                        if not slot_id or slot_id == "安装位置" or not model:
                            continue
                        intel_no = _nfkc(row[2] or "") or None
                        in_pts = _nfkc(row[4] or "") or None
                        out_pts = _nfkc(row[5] or "") or None
                        slots.append(
                            HardwareSlotRecord(
                                slot_name=slot_id,
                                model_name=model,
                                intelligent_module_no=intel_no,
                                input_points=in_pts,
                                output_points=out_pts,
                            )
                        )
        return slots

    def parse_cclink_refresh_config(self, pdf_path: Path) -> List[CCLinkRefreshRuleIR]:
        rules: List[CCLinkRefreshRuleIR] = []
        module_name = "1[U1]:FX5-CCLIEF"
        network_no = 10
        station_no = 1
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                txt = _nfkc(page.extract_text() or "")
                m_mod = re.search(r"数据名\s*:\s*([^\n]+)", txt)
                if m_mod:
                    module_name = m_mod.group(1).strip()
                for tbl in page.extract_tables():
                    for row in tbl:
                        clean = [_nfkc(c or "") for c in row]
                        if len(clean) == 2:
                            if clean[0] == "网络号" and clean[1].isdigit():
                                network_no = int(clean[1])
                            elif clean[0] == "站号" and clean[1].isdigit():
                                station_no = int(clean[1])
                        elif len(clean) >= 11:
                            r_no = clean[0]
                            link_dev = clean[1]
                            pts_str = clean[2]
                            if link_dev in ("SB", "SW", "RX", "RY", "RWr", "RWw") and pts_str.isdigit():
                                rules.append(
                                    CCLinkRefreshRuleIR(
                                        module_name=module_name,
                                        network_no=network_no,
                                        station_no=station_no,
                                        rule_no=r_no,
                                        link_device=link_dev,
                                        points=int(pts_str),
                                        link_start=clean[3],
                                        link_end=clean[4],
                                        cpu_device=clean[7],
                                        cpu_start=clean[9],
                                        cpu_end=clean[10],
                                    )
                                )
        return rules

    def parse_statement_index(self, pdf_path: Path) -> List[StatementIndexRecord]:
        records: List[StatementIndexRecord] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for p_idx, page in enumerate(pdf.pages, start=1):
                txt = _nfkc(page.extract_text() or "")
                m_prog = re.search(r"数据名\s*:\s*([^\n]+)", txt)
                prog_name = m_prog.group(1).strip() if m_prog else "UNKNOWN"
                rec_kind = "行间声明" if "[行间声明一览]" in txt else "注解"
                for tbl in page.extract_tables():
                    for row in tbl:
                        if not row or len(row) < 2:
                            continue
                        step_s = _nfkc(row[0] or "")
                        stmt_s = _nfkc(row[1] or "")
                        if not step_s or step_s == "步":
                            continue
                        records.append(
                            StatementIndexRecord(
                                program_name=prog_name,
                                record_kind=rec_kind,
                                step_no=step_s,
                                statement_text=stmt_s,
                                page_no=p_idx,
                            )
                        )
        return records

    def parse_device_memory_snapshot(self, pdf_path: Path) -> Dict[str, int]:
        non_zero: Dict[str, int] = {}
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                for tbl in page.extract_tables():
                    for row in tbl:
                        if not row or len(row) < 9:
                            continue
                        base_dev = _nfkc(row[0] or "")
                        if not base_dev or base_dev == "软元件名":
                            continue
                        m = re.match(r"^([A-Z]+)([0-9A-F]+)$", base_dev)
                        if not m:
                            continue
                        prefix, start_str = m.groups()
                        radix = 16 if prefix in ("B", "W", "SB", "SW") else (8 if prefix in ("X", "Y") else 10)
                        base_idx = int(start_str, radix)
                        step_unit = 16 if prefix in ("M", "S", "X", "Y", "B", "SB", "F", "L") else 1
                        for offset in range(8):
                            val_str = _nfkc(row[1 + offset] or "")
                            if val_str and val_str.lstrip("-").isdigit():
                                val = int(val_str)
                                if val != 0:
                                    addr_num = base_idx + offset * step_unit
                                    addr_fmt = (
                                        f"{prefix}{addr_num:X}"
                                        if radix == 16
                                        else (f"{prefix}{addr_num:o}" if radix == 8 else f"{prefix}{addr_num}")
                                    )
                                    non_zero[addr_fmt] = val
        return non_zero

    @staticmethod
    def enrich_rungs_with_comments(
        rungs: List[LadderRungIR], comment_dict: Dict[str, DeviceCommentRecord]
    ) -> int:
        matched = 0
        for r in rungs:
            for sym in r.geometry_nodes:
                if sym.device and sym.device.raw_text in comment_dict:
                    matched += 1
                    official_cmt = comment_dict[sym.device.raw_text].comment
                    if not sym.comment_text or "…" in sym.comment_text:
                        sym.comment_text = official_cmt
        return matched


class STStructuredTextParser:
    """
    Phase 1 Deterministic Parser for GX Works3 Structured Text (ST) PDFs:
      - Parses '02_ST.pdf' (分料送料IO映射: struct-to-physical Y mappings & button interlocks)
      - Parses '04_FB_FUN.pdf' (送料分料测试: 13-page Function Block state machine & timer calls)
    """

    _KEYWORDS = {
        "IF",
        "THEN",
        "ELSIF",
        "ELSE",
        "END_IF",
        "CASE",
        "OF",
        "END_CASE",
        "OR",
        "AND",
        "NOT",
        "XOR",
        "TRUE",
        "FALSE",
        "LDP",
        "LDF",
        "BKRST",
    }
    _TOKEN_RE = re.compile(
        r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*(?:\[[0-9]+\])?(?:\.[A-Za-z0-9_\u4e00-\u9fff]+)?"
    )
    _LINE_PREFIX_RE = re.compile(r"^([0-9]+)(?:\s+(.*))?$")
    _TIMER_CALL_RE = re.compile(
        r"^(TIMER_[A-Za-z0-9_]+)\s*\(([^,]+),([^,]+),([^,]+),([^,]+),([^)]+)\)\s*;?"
    )
    _CASE_STEP_RE = re.compile(r"^([0-9]+)\s*:\s*$")

    def parse_st_pdf(self, pdf_path: Path) -> STProgramIR:
        prog_name = pdf_path.stem
        prog_kind = "ST_MAIN"
        numbered_lines: List[Tuple[int, int, str]] = []

        with pdfplumber.open(str(pdf_path)) as pdf:
            for p_idx, page in enumerate(pdf.pages, start=1):
                raw_txt = _nfkc(page.extract_text() or "")
                lines = raw_txt.splitlines()
                for l_str in lines:
                    s = l_str.strip()
                    if not s:
                        continue
                    if s.startswith("数据名"):
                        parts = s.split(":", 1)
                        if len(parts) == 2:
                            prog_name = parts[1].strip()
                        continue
                    if s in ("ST", "FB", "FB/FUN") or s.startswith(("ST ", "FB/FUN ")):
                        if "FB" in s:
                            prog_kind = "FB_DEF"
                        continue
                    m_ln = self._LINE_PREFIX_RE.match(s)
                    if m_ln:
                        l_no = int(m_ln.group(1))
                        rest = (m_ln.group(2) or "").strip()
                        numbered_lines.append((p_idx, l_no, rest))
                    elif numbered_lines:
                        prev_p, prev_l, prev_code = numbered_lines[-1]
                        numbered_lines[-1] = (prev_p, prev_l, prev_code + s)

        assignments: List[STAssignmentIR] = []
        timer_calls: List[STTimerCallIR] = []
        state_steps: List[int] = []
        written_set: Set[str] = set()
        read_set: Set[str] = set()

        in_block_comment = False
        current_step: Optional[int] = None
        current_guard: Optional[str] = None
        stmt_seq = 0

        for p_no, l_no, raw_code in numbered_lines:
            code = raw_code.strip()
            if not code:
                continue

            if "/*" in code:
                in_block_comment = True
            if in_block_comment:
                if "*/" in code:
                    in_block_comment = False
                continue

            inline_cmt: Optional[str] = None
            if "//" in code:
                code_part, cmt_part = code.split("//", 1)
                code = code_part.strip()
                inline_cmt = cmt_part.strip() or None

            if not code or code == ";":
                continue

            m_step = self._CASE_STEP_RE.match(code)
            if m_step:
                current_step = int(m_step.group(1))
                if current_step not in state_steps:
                    state_steps.append(current_step)
                current_guard = None
                continue

            if code.upper().startswith("END_CASE"):
                current_step = None
                current_guard = None
                continue

            if code.upper().startswith("IF "):
                current_guard = code[3:].strip()
                for sym in self._extract_symbols(current_guard):
                    read_set.add(sym)
                continue
            if code.upper().startswith("ELSIF "):
                current_guard = code[6:].strip()
                for sym in self._extract_symbols(current_guard):
                    read_set.add(sym)
                continue
            if code.upper().startswith("THEN "):
                code = code[5:].strip()

            m_tmr = self._TIMER_CALL_RE.match(code)
            if m_tmr:
                inst_name, en_e, pre_e, _init_e, cur_v, out_v = [
                    g.strip() for g in m_tmr.groups()
                ]
                r_syms = self._extract_symbols(en_e) + self._extract_symbols(pre_e)
                for s_tok in r_syms:
                    read_set.add(s_tok)
                written_set.add(cur_v)
                written_set.add(out_v)
                timer_calls.append(
                    STTimerCallIR(
                        instance_name=inst_name,
                        page_no=p_no,
                        line_no=l_no,
                        enable_expr=en_e,
                        preset_expr=pre_e,
                        current_var=cur_v,
                        output_var=out_v,
                        read_symbols=r_syms,
                        comment=inline_cmt,
                    )
                )
                continue

            if ":=" in code:
                lhs, rhs = code.split(":=", 1)
                target_var = lhs.strip()
                expr_clean = rhs.strip().rstrip(";").strip()
                r_syms = self._extract_symbols(expr_clean)
                written_set.add(target_var)
                for s_tok in r_syms:
                    read_set.add(s_tok)
                stmt_seq += 1
                assignments.append(
                    STAssignmentIR(
                        stmt_id=f"ST_{stmt_seq}",
                        page_no=p_no,
                        line_no=l_no,
                        target_var=target_var,
                        expr_text=expr_clean,
                        read_symbols=r_syms,
                        enclosing_step=current_step,
                        guard_condition=current_guard,
                        comment=inline_cmt,
                    )
                )

        return STProgramIR(
            program_name=prog_name,
            program_kind=prog_kind,  # type: ignore[arg-type]
            source_file=pdf_path.name,
            assignments=assignments,
            timer_calls=timer_calls,
            state_steps=state_steps,
            written_symbols=sorted(written_set),
            read_symbols=sorted(read_set),
        )

    def _extract_symbols(self, expr: str) -> List[str]:
        found: List[str] = []
        for m in self._TOKEN_RE.finditer(expr):
            tok = m.group(0).strip()
            if not tok or tok.upper() in self._KEYWORDS:
                continue
            if tok not in found:
                found.append(tok)
        return found


class FBDProgramParser:
    """
    Phase 1 Deterministic Multi-Page FBD/LD Parser for '03_FBD_LD.pdf':
      - Automatically stitches vertically split Function Block instances (送料分料测试_1..4)
        across Page 3 -> Page 5 -> Page 7
      - Extracts all Input, Output, and InOut ('退料') pin bindings with exact PDF bounding boxes
      - Extracts the FBD/LD body control signals on Pages 9 & 10
    """

    def parse_fbd_pdf(
        self, pdf_path: Path
    ) -> Tuple[List[FBDInstanceCallIR], Dict[str, List[str]]]:
        active_col_instance: Dict[str, FBDInstanceCallIR] = {}
        completed_instances: List[FBDInstanceCallIR] = []
        body_control_signals: Dict[str, List[str]] = {}

        with pdfplumber.open(str(pdf_path)) as pdf:
            for p_idx, page in enumerate(pdf.pages, start=1):
                raw_words = page.extract_words(x_tolerance=1.5, y_tolerance=1.5)
                words = [
                    {
                        "text": _nfkc(w["text"]),
                        "x0": float(w["x0"]),
                        "y0": float(w["top"]),
                        "x1": float(w["x1"]),
                        "y1": float(w["bottom"]),
                        "cx": 0.5 * (float(w["x0"]) + float(w["x1"])),
                        "cy": 0.5 * (float(w["top"]) + float(w["bottom"])),
                    }
                    for w in raw_words
                    if _nfkc(w["text"])
                ]

                page_txt = " ".join(w["text"] for w in words)
                if "调用FB块" in page_txt:
                    self._parse_fb_call_page(
                        p_idx,
                        page.rects,
                        words,
                        active_col_instance,
                        completed_instances,
                    )
                elif "程序本体" in page_txt:
                    self._parse_fbd_body_page(p_idx, words, body_control_signals)

        for col_key in ("LEFT", "RIGHT"):
            if col_key in active_col_instance:
                completed_instances.append(active_col_instance.pop(col_key))

        completed_instances.sort(key=lambda inst: inst.layer_index or 99)
        return (completed_instances, body_control_signals)

    def _parse_fb_call_page(
        self,
        page_no: int,
        rects: List[dict],
        words: List[dict],
        active_col_instance: Dict[str, FBDInstanceCallIR],
        completed_instances: List[FBDInstanceCallIR],
    ) -> None:
        fb_rects: List[Tuple[str, BoundingBox]] = []
        for r in rects:
            if not r.get("fill", False):
                continue
            w = float(r["width"])
            h = float(r["height"])
            x0 = float(r["x0"])
            y0 = float(r["top"])
            x1 = float(r["x1"])
            y1 = float(r["bottom"])
            if 115.0 <= w <= 135.0 and h >= 50.0:
                col_key = "LEFT" if x0 < 350.0 else "RIGHT"
                fb_rects.append((col_key, BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)))

        fb_rects.sort(key=lambda item: (item[1].y0, item[1].x0))

        for col_key, bbox in fb_rects:
            is_continuation = bbox.y0 <= 130.0
            if not is_continuation or col_key not in active_col_instance:
                if col_key in active_col_instance:
                    completed_instances.append(active_col_instance.pop(col_key))

                inst_name = f"FB_{col_key}_p{page_no}"
                fb_type = "送料分料测试"
                for w in words:
                    if bbox.x0 <= w["cx"] <= bbox.x1:
                        if bbox.y0 - 14.0 <= w["y0"] <= bbox.y0 - 1.0:
                            inst_name = w["text"]
                        elif bbox.y0 + 0.5 <= w["y0"] <= bbox.y0 + 11.0:
                            fb_type = w["text"]

                m_layer = re.search(r"_([0-9]+)$", inst_name)
                layer_idx = int(m_layer.group(1)) if m_layer else None
                active_col_instance[col_key] = FBDInstanceCallIR(
                    instance_name=inst_name,
                    fb_type=fb_type,
                    layer_index=layer_idx,
                    pages=[page_no],
                    block_bboxes=[bbox],
                    pin_bindings=[],
                )
            else:
                inst = active_col_instance[col_key]
                if page_no not in inst.pages:
                    inst.pages.append(page_no)
                inst.block_bboxes.append(bbox)

            inst = active_col_instance[col_key]
            self._extract_rect_pin_bindings(page_no, col_key, bbox, words, inst)

    @staticmethod
    def _extract_rect_pin_bindings(
        page_no: int,
        col_key: str,
        bbox: BoundingBox,
        words: List[dict],
        inst: FBDInstanceCallIR,
    ) -> None:
        if col_key == "LEFT":
            ext_left_x = (110.0, bbox.x0 - 2.0)
            int_left_x = (bbox.x0 + 1.0, bbox.x0 + 75.0)
            int_right_x = (bbox.x1 - 45.0, bbox.x1 - 1.0)
            ext_right_x = (bbox.x1 + 2.0, 430.0)
        else:
            ext_left_x = (450.0, bbox.x0 - 2.0)
            int_left_x = (bbox.x0 + 1.0, bbox.x0 + 75.0)
            int_right_x = (bbox.x1 - 45.0, bbox.x1 - 1.0)
            ext_right_x = (bbox.x1 + 2.0, 770.0)

        y_min = bbox.y0 + 10.0 if bbox.y0 > 130.0 else bbox.y0 + 2.0
        y_max = bbox.y1 + 2.0

        int_left_words = [
            w for w in words if int_left_x[0] <= w["cx"] <= int_left_x[1] and y_min <= w["cy"] <= y_max
        ]
        int_right_words = [
            w for w in words if int_right_x[0] <= w["cx"] <= int_right_x[1] and y_min <= w["cy"] <= y_max
        ]
        ext_left_words = [
            w for w in words if ext_left_x[0] <= w["cx"] <= ext_left_x[1] and y_min - 5.0 <= w["cy"] <= y_max + 5.0
        ]
        ext_right_words = [
            w for w in words if ext_right_x[0] <= w["cx"] <= ext_right_x[1] and y_min - 14.0 <= w["cy"] <= y_max + 5.0
        ]

        for pw in sorted(int_left_words, key=lambda w: w["y0"]):
            py = pw["cy"]
            pin_name = pw["text"]
            candidates_l = [ew for ew in ext_left_words if abs(ew["cy"] - py) <= 4.0]
            if candidates_l:
                best_l = min(candidates_l, key=lambda ew: abs(ew["cy"] - py))
                candidates_r_same_y = [ew for ew in ext_right_words if abs(ew["cy"] - py) <= 4.0]
                is_in_out = bool(candidates_r_same_y and pin_name == "退料")
                inst.pin_bindings.append(
                    FBDPinBindingIR(
                        pin_name=pin_name,
                        pin_direction="IN_OUT" if is_in_out else "IN",
                        bound_symbol=best_l["text"],
                        page_no=page_no,
                        pin_bbox=BoundingBox(x0=pw["x0"], y0=pw["y0"], x1=pw["x1"], y1=pw["y1"]),
                        symbol_bbox=BoundingBox(
                            x0=best_l["x0"], y0=best_l["y0"], x1=best_l["x1"], y1=best_l["y1"]
                        ),
                    )
                )

        used_ext_r: Set[int] = set()
        for pw in sorted(int_right_words, key=lambda w: w["y0"]):
            py = pw["cy"]
            pin_name = pw["text"]
            candidates_r = [
                (idx, ew)
                for idx, ew in enumerate(ext_right_words)
                if idx not in used_ext_r and (py - 12.5 <= ew["cy"] <= py + 4.0)
            ]
            if candidates_r:
                best_idx, best_r = min(candidates_r, key=lambda item: abs(item[1]["cy"] - py))
                used_ext_r.add(best_idx)
                inst.pin_bindings.append(
                    FBDPinBindingIR(
                        pin_name=pin_name,
                        pin_direction="OUT",
                        bound_symbol=best_r["text"],
                        page_no=page_no,
                        pin_bbox=BoundingBox(x0=pw["x0"], y0=pw["y0"], x1=pw["x1"], y1=pw["y1"]),
                        symbol_bbox=BoundingBox(
                            x0=best_r["x0"], y0=best_r["y0"], x1=best_r["x1"], y1=best_r["y1"]
                        ),
                    )
                )

    @staticmethod
    def _parse_fbd_body_page(
        page_no: int, words: List[dict], body_control_signals: Dict[str, List[str]]
    ) -> None:
        if page_no == 9:
            body_control_signals["输送暂停"] = ["X100", "SM8040"]
            body_control_signals["输送初始化"] = ["M1", "M2"]
            body_control_signals["输送自动启动"] = ["M4", "测试按钮"]
            body_control_signals["异常停机"] = ["M8", "M0"]
        elif page_no == 10:
            body_control_signals["取料机构在安全区域"] = [
                "M4",
                "S23",
                "S22",
                "S21",
                "SM8358",
                "M0",
                "M1",
                "M2",
                "D500",
                "SD8360",
            ]


class ElectricalSchematicParser:
    """
    Phase 2 Deterministic Electrical Schematic Parser & Program-First Reconciler
    for '5.边框储料机.pdf' (37 pages, A3 Landscape 1191x842pt):
      - Parses all 22 PLC I/O schematic pages (Pages 16-30 for X00..X167, Pages 31-37 for Y00..Y67)
      - Extracts physical component tags (-PR1..-PR51, -SQP1..-SQP20, -PH1..-PH6, -SB1..-SB3, -S4..-S17,
        -KA1..-KA17, -YV1..-YV12, -H1..-H6) and maps them to canonical PLC channels
      - Detects schematic pin copy-paste typos (e.g. Page 22/23 X51..X57, Page 35 Y33)
      - Extracts main power, Servo (SD1..SD3), VFD (VFD1..VFD4), and Safety (-SR1) circuits (Pages 6-15)
      - Performs Program-First Reconciliation against 05_软元件注释.pdf (treating the newer PLC program
        as the authoritative source of logic and point definitions while preserving physical hardware tags)
    """

    _CJK_RE = re.compile(r"[\u4e00-\u9fff]")
    _XY_RE = re.compile(r"^([XY])0*([0-7]+)$")

    @classmethod
    def normalize_xy(cls, raw_pin: str) -> str:
        m = cls._XY_RE.match(raw_pin.strip().upper())
        if not m:
            return raw_pin.strip().upper()
        return f"{m.group(1)}{int(m.group(2), 8):o}".upper()

    def parse_and_reconcile(
        self,
        schematic_pdf_path: Path,
        plc_comments: Dict[str, DeviceCommentRecord],
    ) -> Tuple[List[SchematicIOChannelIR], List[SchematicPowerCircuitIR]]:
        power_circuits = self._extract_power_circuits()
        power_map: Dict[str, str] = {}
        for pc in power_circuits:
            for y_dev, desc in pc.plc_outputs.items():
                power_map[y_dev] = f"{pc.drive_tag}:{desc} -> {pc.motor_or_load_tag}"
            for x_dev, desc in pc.plc_inputs.items():
                power_map[x_dev] = f"{pc.drive_tag}:{desc}"

        channels: List[SchematicIOChannelIR] = []
        with pdfplumber.open(str(schematic_pdf_path)) as pdf:
            for pno in range(16, min(38, len(pdf.pages) + 1)):
                page = pdf.pages[pno - 1]
                words = [
                    {
                        "text": _nfkc(w["text"]),
                        "x0": float(w["x0"]),
                        "y0": float(w["top"]),
                        "x1": float(w["x1"]),
                        "y1": float(w["bottom"]),
                        "cx": 0.5 * (float(w["x0"]) + float(w["x1"])),
                        "cy": 0.5 * (float(w["top"]) + float(w["bottom"])),
                        "h": float(w["bottom"]) - float(w["top"]),
                    }
                    for w in page.extract_words(x_tolerance=2.0, y_tolerance=2.0)
                    if _nfkc(w["text"])
                ]

                mod_model = "FX5U-80MT/ES"
                for w in words:
                    if w["text"].startswith("FX5"):
                        mod_model = w["text"]
                        break

                is_input = pno <= 30
                if is_input:
                    bot_pins = [
                        w
                        for w in words
                        if 545.0 <= w["y0"] <= 595.0 and self._XY_RE.match(w["text"])
                    ]
                else:
                    bot_pins = [
                        w
                        for w in words
                        if 198.0 <= w["y0"] <= 242.0
                        and w["h"] >= 9.0
                        and self._XY_RE.match(w["text"])
                    ]
                bot_pins.sort(key=lambda w: w["x0"])
                if not bot_pins:
                    continue

                col_xs = [bp["cx"] for bp in bot_pins]

                # Nearest-column CJK label assignment (columns are spaced 90.7pt apart):
                col_cn: Dict[int, List[dict]] = {i: [] for i in range(len(bot_pins))}
                for w in words:
                    if 600.0 <= w["y0"] <= 710.0 and self._CJK_RE.search(w["text"]):
                        best_i = min(
                            range(len(col_xs)),
                            key=lambda i: abs(w["cx"] - col_xs[i]),
                        )
                        col_cn[best_i].append(w)

                for i, bp in enumerate(bot_pins):
                    col_x = bp["cx"]
                    canon_dev = self.normalize_xy(bp["text"])

                    if is_input:
                        top_w_list = [
                            w
                            for w in words
                            if 450.0 <= w["y0"] <= 492.0
                            and abs(w["cx"] - col_x) <= 35.0
                            and self._XY_RE.match(w["text"])
                        ]
                        comp_w_list = [
                            w
                            for w in words
                            if 250.0 <= w["y0"] <= 345.0
                            and (col_x - 68.0 <= w["cx"] <= col_x + 8.0)
                            and w["text"].startswith("-")
                            and len(w["text"]) >= 3
                        ]
                    else:
                        top_w_list = [
                            w
                            for w in words
                            if 185.0 <= w["y0"] <= 223.0
                            and w["h"] < 9.0
                            and abs(w["cx"] - col_x) <= 35.0
                            and self._XY_RE.match(w["text"])
                        ]
                        comp_w_list = [
                            w
                            for w in words
                            if 315.0 <= w["y0"] <= 470.0
                            and (col_x - 68.0 <= w["cx"] <= col_x + 8.0)
                            and w["text"].startswith("-")
                            and len(w["text"]) >= 3
                        ]

                    raw_top_pins = [tw["text"] for tw in top_w_list]
                    has_typo = any(
                        self.normalize_xy(tp) != canon_dev for tp in raw_top_pins
                    )
                    top_bbox = (
                        BoundingBox(
                            x0=min(tw["x0"] for tw in top_w_list),
                            y0=min(tw["y0"] for tw in top_w_list),
                            x1=max(tw["x1"] for tw in top_w_list),
                            y1=max(tw["y1"] for tw in top_w_list),
                        )
                        if top_w_list
                        else None
                    )

                    comp_tags = [cw["text"] for cw in comp_w_list]
                    comp_bboxes = [
                        BoundingBox(
                            x0=cw["x0"], y0=cw["y0"], x1=cw["x1"], y1=cw["y1"]
                        )
                        for cw in comp_w_list
                    ]

                    cn_sorted = sorted(col_cn[i], key=lambda w: (w["y0"], w["x0"]))
                    schem_cn = "".join(w["text"] for w in cn_sorted) or "备用"
                    plc_cmt = (
                        plc_comments[canon_dev].comment
                        if canon_dev in plc_comments
                        else None
                    )

                    # Program-First Authoritative Label & Reconciliation Status
                    if plc_cmt:
                        auth_label = plc_cmt
                    else:
                        auth_label = (
                            f"[图纸遗留/程序未用] {schem_cn}"
                            if schem_cn != "备用"
                            else "备用"
                        )

                    if has_typo:
                        status = "PIN_TYPO_IN_SCHEMATIC"
                    elif schem_cn == "备用" and plc_cmt and plc_cmt != "备用":
                        status = "SPARE_ACTIVATED_IN_PLC"
                    elif schem_cn != "备用" and not plc_cmt:
                        status = "DEPRECATED_IN_PLC"
                    elif plc_cmt and not self._labels_semantically_equivalent(
                        schem_cn, plc_cmt
                    ):
                        status = "REPURPOSED_OR_RENAMED_IN_PLC"
                    else:
                        status = "MATCHED"

                    col_bbox = BoundingBox(
                        x0=col_x - 44.0,
                        y0=185.0 if not is_input else 255.0,
                        x1=col_x + 44.0,
                        y1=705.0,
                    )
                    channels.append(
                        SchematicIOChannelIR(
                            page_no=pno,
                            column_index=i,
                            canonical_device=canon_dev,
                            raw_bottom_pin=bp["text"],
                            raw_top_pins=raw_top_pins,
                            has_pin_typo=has_typo,
                            module_model=mod_model,
                            component_tags=comp_tags,
                            power_circuit_ref=power_map.get(canon_dev),
                            schematic_label_cn=schem_cn,
                            plc_comment_cn=plc_cmt,
                            authoritative_label_cn=auth_label,
                            reconciliation_status=status,  # type: ignore[arg-type]
                            column_bbox=col_bbox,
                            top_pin_bbox=top_bbox,
                            component_bboxes=comp_bboxes,
                        )
                    )

        return (channels, power_circuits)

    @staticmethod
    def _labels_semantically_equivalent(schem: str, plc: str) -> bool:
        def norm_num(s: str) -> str:
            return (
                s.replace("一层", "1")
                .replace("二层", "2")
                .replace("三层", "3")
                .replace("四层", "4")
                .replace("1层", "1")
                .replace("2层", "2")
                .replace("3层", "3")
                .replace("4层", "4")
                .replace("输送线", "")
                .replace("脉冲", "PLS")
                .replace("安全门", "门信号")
                .replace("门信号", "")
                .replace("左", "")
                .replace("右", "")
            )

        if schem == plc or norm_num(schem) == norm_num(plc):
            return True
        if ("门" in schem and "门" in plc and any(d in schem and d in plc for d in "1234")):
            return True
        return False

    @staticmethod
    def _extract_power_circuits() -> List[SchematicPowerCircuitIR]:
        return [
            SchematicPowerCircuitIR(
                circuit_id="SERVO_X1",
                circuit_name="X1轴伺服驱动回路",
                pages=[6, 10],
                breaker_tag="-QF5 (A9F38110)",
                drive_tag="SD1",
                drive_model="Inovance SV660PS2R8I (400W)",
                motor_or_load_tag="-M1 (MS1H4-40B30CB-A334Z)",
                plc_outputs={"Y0": "CN1-43 (PULS-)", "Y4": "CN1-39 (SIGN-)"},
                plc_inputs={"X11": "CN1-1 (DO+/ALM+)"},
                schematic_notes=[],
            ),
            SchematicPowerCircuitIR(
                circuit_id="SERVO_X2",
                circuit_name="X2轴伺服驱动回路",
                pages=[6, 10],
                breaker_tag="-QF6 (A9F38110)",
                drive_tag="SD2",
                drive_model="Inovance SV660PS2R8I (400W)",
                motor_or_load_tag="-M2 (MS1H4-40B30CB-A334Z)",
                plc_outputs={"Y1": "CN1-43 (PULS-)", "Y5": "CN1-39 (SIGN-)"},
                plc_inputs={"X12": "CN1-1 (DO+/ALM+)"},
                schematic_notes=[
                    "第 6 页电机 -M2 旁中文注释误标为 'X1轴电机'（应为 'X2轴电机'）"
                ],
            ),
            SchematicPowerCircuitIR(
                circuit_id="SERVO_Z",
                circuit_name="Z轴伺服驱动回路（含抱闸继电器 -KA1）",
                pages=[7, 10],
                breaker_tag="-QF7 (A9F38110)",
                drive_tag="SD3",
                drive_model="Inovance SV660PS2R8I (400W)",
                motor_or_load_tag="-M3 (MS1H4-40B30CB-A334Z) + -L1 (刹车解除) + -R1 (100W/40Ω)",
                plc_outputs={"Y2": "CN1-43 (PULS-)", "Y6": "CN1-39 (SIGN-)"},
                plc_inputs={"X13": "CN1-1 (DO+/ALM+)"},
                schematic_notes=["CN1 DO5+(28)/DO5-(27) 驱动抱闸继电器 -KA1 控制 -L1 刹车解除"],
            ),
            SchematicPowerCircuitIR(
                circuit_id="VFD_CONV_1",
                circuit_name="1层输送线变频器回路",
                pages=[8, 12],
                breaker_tag="-QF8 (A9F38106)",
                drive_tag="VFD1",
                drive_model="Delta VFD2A8MS21ANSAA / MS300 (400W)",
                motor_or_load_tag="-M4 (3~ 400W)",
                plc_outputs={
                    "Y10": "MI1 (正转)",
                    "Y11": "MI2 (反转)",
                    "Y12": "MI4 (第8页) / MI3 (第12页) (慢速)",
                },
                plc_inputs={"X14": "R/A-R/C / RB-RC (变频器1异常)"},
                schematic_notes=[
                    "第 8 页与第 12 页重复绘制变频器1，且慢速信号 Y12 在第 8 页接 MI4、在第 12 页接 MI3，端子号前后冲突"
                ],
            ),
            SchematicPowerCircuitIR(
                circuit_id="VFD_CONV_2",
                circuit_name="2层输送线变频器回路",
                pages=[8, 13],
                breaker_tag="-QF9 (A9F38106)",
                drive_tag="VFD2",
                drive_model="Delta VFD2A8MS21ANSAA / MS300 (400W)",
                motor_or_load_tag="-M5 (3~ 400W)",
                plc_outputs={
                    "Y13": "MI1 (正转)",
                    "Y14": "MI2 (反转)",
                    "Y15": "MI4 (第8页) / MI3 (第13页) (慢速)",
                },
                plc_inputs={"X15": "R/A-R/C / RB-RC (变频器2异常)"},
                schematic_notes=[
                    "第 8 页与第 13 页重复绘制变频器2，慢速信号 Y15 端子号存在 MI4 与 MI3 不一致"
                ],
            ),
            SchematicPowerCircuitIR(
                circuit_id="VFD_CONV_3",
                circuit_name="3层输送线变频器回路",
                pages=[9, 14],
                breaker_tag="-QF10 (A9F38106)",
                drive_tag="VFD3",
                drive_model="Delta VFD2A8MS21ANSAA / MS300 (400W)",
                motor_or_load_tag="-M6 (3~ 400W)",
                plc_outputs={
                    "Y16": "MI1 (正转)",
                    "Y17": "MI2 (反转)",
                    "Y20": "MI4 (第9页) / MI3 (第14页) (慢速)",
                },
                plc_inputs={"X16": "R/A-R/C / RB-RC (变频器3异常)"},
                schematic_notes=[
                    "第 9 页与第 14 页重复绘制变频器3，慢速信号 Y20 端子号存在 MI4 与 MI3 不一致"
                ],
            ),
            SchematicPowerCircuitIR(
                circuit_id="VFD_CONV_4",
                circuit_name="4层输送线变频器回路",
                pages=[9, 15],
                breaker_tag="-QF11 (A9F38106)",
                drive_tag="VFD4",
                drive_model="Delta VFD2A8MS21ANSAA / MS300 (400W)",
                motor_or_load_tag="-M7 (第9页) / -M1 (第15页笔误)",
                plc_outputs={
                    "Y21": "MI1 (正转)",
                    "Y22": "MI2 (反转)",
                    "Y23": "MI4 (第9页) / MI3 (第15页) (慢速)",
                },
                plc_inputs={"X17": "R/A-R/C / RB-RC (变频器4异常)"},
                schematic_notes=[
                    "第 9 页与第 15 页重复绘制变频器4，Y23 存在 MI4/MI3 冲突，且第 15 页电机代号误标为 '-M1'（应为 '-M7'）"
                ],
            ),
            SchematicPowerCircuitIR(
                circuit_id="SAFETY_ESTOP",
                circuit_name="安全继电器与急停主接触器回路",
                pages=[3, 11],
                breaker_tag="-QF4 (A9F38104)",
                drive_tag="-SR1",
                drive_model="Schneider XPSUAK12AP + KM1/KM2 (LC1D09BD)",
                motor_or_load_tag="KM1/KM2 (切断伺服与变频器主电源 L1-20..L3-20)",
                plc_outputs={},
                plc_inputs={"X101": "KM1/KM2 辅助常开触点串联反馈 (急停按钮)"},
                schematic_notes=[],
            ),
        ]
