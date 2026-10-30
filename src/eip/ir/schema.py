from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Field


class SourceModality(str, Enum):
    VECTOR = "vector"
    OCR_FALLBACK = "ocr_fallback"
    HYBRID = "hybrid"
    CSV_IMPORT = "csv_import"
    MANUAL_OVERRIDE = "manual_override"


class BoundingBox(BaseModel):
    """PDF native unscaled Point coordinate system (top-left origin, 1pt = 1/72 inch)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def expand(self, margin: float) -> "BoundingBox":
        return BoundingBox(
            x0=self.x0 - margin,
            y0=self.y0 - margin,
            x1=self.x1 + margin,
            y1=self.y1 + margin,
        )


class EvidenceAnchor(BaseModel):
    """Mandatory provenance anchor linking any IR entity back to the raw PDF coordinates."""
    doc_sha256: str
    page_no: int = Field(..., ge=1)
    bbox: BoundingBox
    source_modality: SourceModality = SourceModality.VECTOR
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    raw_primitive_ids: List[str] = Field(default_factory=list)


class RawVectorLine(BaseModel):
    line_id: str
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    stroke_width: float = 0.5

    @property
    def bbox(self) -> BoundingBox:
        return BoundingBox(
            x0=min(self.x0, self.x1),
            y0=min(self.y0, self.y1),
            x1=max(self.x0, self.x1),
            y1=max(self.y0, self.y1),
        )


class RawVectorRect(BaseModel):
    rect_id: str
    page_no: int
    bbox: BoundingBox
    stroke_width: float = 0.5
    is_filled: bool = False


class RawVectorCurve(BaseModel):
    curve_id: str
    page_no: int
    points: List[Tuple[float, float]]
    bbox: BoundingBox
    stroke_width: float = 0.5


class RawTextSpan(BaseModel):
    text_id: str
    page_no: int
    text: str
    bbox: BoundingBox
    font_name: str = "Unknown"
    font_size: float = 8.0
    has_garbled_glyphs: bool = False


class PageModalityProfile(BaseModel):
    """L1 Modality Profiler output including characteristic geometric scales."""
    doc_sha256: str
    page_no: int
    page_width: float
    page_height: float
    modality: Literal["VECTOR", "RASTER", "HYBRID"]
    vector_line_count: int
    text_span_count: int
    garbled_span_count: int
    raster_coverage_ratio: float
    characteristic_contact_width_pt: float
    characteristic_stroke_width_pt: float
    tolerance_collinear_pt: float
    tolerance_snap_pt: float
    tolerance_symbol_guard_pt: float
    garbled_bboxes: List[BoundingBox] = Field(default_factory=list)


class GeometryMergeRecord(BaseModel):
    """Audit record for every non-zero geometric endpoint snap or collinear stitch."""
    merge_id: str
    page_no: int
    primitive_a_id: str
    primitive_b_id: str
    point_a: Tuple[float, float]
    point_b: Tuple[float, float]
    euclidean_distance_pt: float
    applied_tolerance_pt: float
    rule_name: Literal["collinear_stitch", "endpoint_snap", "t_junction_snap"]


class TypedDeviceIR(BaseModel):
    """Structured Mitsubishi FX5U device or constant operand representation."""
    raw_text: str
    device_type: str
    base_address: str
    radix: Literal[8, 10, 16]
    address_int: int = 0
    bit_index: Optional[int] = None
    digit_k: Optional[int] = None
    index_reg: Optional[str] = None
    data_type: Literal["BOOL", "BIT_ARRAY", "WORD", "DWORD", "TIMER", "COUNTER", "CONSTANT"]
    is_constant: bool = False
    cpu_profile: str = "FX5U"
    is_valid: bool = True
    validation_error: Optional[str] = None


class LadderSymbolNode(BaseModel):
    """Symbol node recognized in the Ladder Geometry Graph."""
    symbol_id: str
    symbol_kind: Literal[
        "NO_CONTACT",
        "NC_CONTACT",
        "RISE_CONTACT",
        "FALL_CONTACT",
        "OUT_COIL",
        "SET_COIL",
        "RST_COIL",
        "FUNC_BLOCK",
        "INV_LINE",
    ]
    mnemonic: str
    device: Optional[TypedDeviceIR] = None
    operands: List[Union[TypedDeviceIR, str]] = Field(default_factory=list)
    comment_text: Optional[str] = None
    left_port_coord: Tuple[float, float]
    right_port_coord: Tuple[float, float]
    anchor: EvidenceAnchor


class GeometryEdge(BaseModel):
    """Directed edge in the Rung Geometry Graph."""
    edge_id: str
    source_node: str
    target_node: str
    symbol: LadderSymbolNode


class InstructionStep(BaseModel):
    """Explicit ladder instruction sequence item including MPS/MRD/MPP stack operations."""
    step_no: int
    opcode: str
    operands: List[str] = Field(default_factory=list)
    source_symbol_id: Optional[str] = None
    stack_depth_after: int = Field(0, ge=0, le=11)


class LogicExprNode(BaseModel):
    """Recursive pure Boolean / comparison condition AST node."""
    expr_type: Literal["ATOM", "NOT", "AND", "OR", "RISE_EDGE", "FALL_EDGE", "COMPARE", "TRUE"]
    operator: Optional[str] = None
    device_ref: Optional[TypedDeviceIR] = None
    symbol_id: Optional[str] = None
    children: List["LogicExprNode"] = Field(default_factory=list)
    anchor_ids: List[str] = Field(default_factory=list)

    def to_canonical_str(self) -> str:
        if self.expr_type == "TRUE":
            return "TRUE"
        if self.expr_type == "ATOM":
            dev = self.device_ref.raw_text if self.device_ref else (self.operator or "UNKNOWN")
            return dev
        if self.expr_type == "RISE_EDGE":
            dev = self.device_ref.raw_text if self.device_ref else "UNKNOWN"
            return f"RISE({dev})"
        if self.expr_type == "FALL_EDGE":
            dev = self.device_ref.raw_text if self.device_ref else "UNKNOWN"
            return f"FALL({dev})"
        if self.expr_type == "NOT":
            inner = self.children[0].to_canonical_str() if self.children else ""
            return f"NOT({inner})"
        if self.expr_type in ("AND", "OR"):
            parts = [c.to_canonical_str() for c in self.children]
            return f"{self.expr_type}(" + ", ".join(parts) + ")"
        return self.expr_type


LogicExprNode.update_forward_refs()


class GuardedStatement(BaseModel):
    """Ordered guarded execution statement decoupling MPS/MRD/MPP branches."""
    exec_order: int
    enable_condition: LogicExprNode
    action_kind: Literal["COIL_OUT", "COIL_SET", "COIL_RST", "FUNC_EXEC"]
    mnemonic: str
    target_device: Optional[TypedDeviceIR] = None
    operands: List[Union[TypedDeviceIR, str]] = Field(default_factory=list)
    is_stateful: bool = False
    anchor: EvidenceAnchor


class LadderRungIR(BaseModel):
    """Complete 3-layer IR for a single Ladder Diagram Rung."""
    rung_id: str
    rung_index: int
    step_range: Optional[Tuple[int, int]] = None
    rung_comment: Optional[str] = None
    junction_coords: Dict[str, Tuple[float, float]] = Field(default_factory=dict)
    left_rail_junction: str = "J_LEFT"
    right_rail_junction: str = "J_RIGHT"
    geometry_nodes: List[LadderSymbolNode] = Field(default_factory=list)
    geometry_edges: List[GeometryEdge] = Field(default_factory=list)
    merge_audit_logs: List[GeometryMergeRecord] = Field(default_factory=list)
    instruction_sequence: List[InstructionStep] = Field(default_factory=list)
    guarded_statements: List[GuardedStatement] = Field(default_factory=list)
    verification_status: Literal["VERIFIED", "WARNING", "FAILED", "PENDING_HUMAN"] = "PENDING_HUMAN"
    diagnostics: List[str] = Field(default_factory=list)
    anchor: EvidenceAnchor
