from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
import pdfplumber

from eip.ir.schema import LadderRungIR, TypedDeviceIR
from eip.plc.lexer import FX5UDeviceLexer


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


class GX3ProjectTableParser:
    """
    Cross-Document Ground Truth Extractor for GX Works3 exported PDFs:
      - Parses '05_软元件注释.pdf' into a validated FX5U Device -> Comment dictionary
      - Parses '07_系统参数.pdf' into the PLC Hardware Rack / I/O Slot table
      - Cross-references and enriches parsed LadderRungIR symbols with full comments
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
                        dev_raw = (row[0] or "").strip()
                        cmt_raw = (row[1] or "").strip()
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
                        slot_id = (row[0] or "").strip()
                        model = (row[1] or "").strip()
                        if not slot_id or slot_id == "安装位置" or not model:
                            continue
                        intel_no = (row[2] or "").strip() or None
                        in_pts = (row[4] or "").strip() or None
                        out_pts = (row[5] or "").strip() or None
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
