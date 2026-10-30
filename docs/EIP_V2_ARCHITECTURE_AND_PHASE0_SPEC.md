# EIP V2 架构冻结基线与 Phase 0 实测执行规范

## 1. 文档定位与 Agent 执行铁律

本文档是 **Electrical Intelligence Platform (EIP) V2** 的唯一架构基线与 Phase 0 开发规范，供开发 Agent（Antigravity / Claude Code / Codex）在独立 Git Worktree 中执行。所有历史讨论中已被否决的方案（如核心链路引入 Marker、全局硬编码 `1.5pt` 容差、强制将所有梯形图归约为无状态简单串并联布尔树）均以此文档为准予以废弃。

### 1.1 核心工程红线（Hard Constraints）

1. **禁止跨越阶段开发**：在 **Phase 0（P0-01 ～ P0-04）** 实测门禁全部通过并由工程师签字确认前，**严禁启动完整 Phase 1 业务模块（如 Review UI、电气原理图跨页链接器或云端 AI Copilot）的编码**。
2. **原始 PDF 是唯一事实来源（Single Source of Truth）**：所有提取出的图元、文本、触点、线圈、指令框、导线节点，必须 100% 携带基于 PDF 原生 Point 坐标系（左上角原点，未缩放 `1pt = 1/72 inch`）的 `EvidenceAnchor` 证据锚点。
3. **几何清洗与合并可回溯**：严禁静默覆盖原始图元坐标。任何共线合并、微间隙吸附（Endpoint Snapping）、网格对齐操作，必须保留原始坐标并写入 `GeometryMergeRecord` 审计日志。
4. **视觉模型与 LLM 读写隔离**：
   * 针对原生导出矢量 PDF，100% 采用基于 [PyMuPDF](https://github.com/pymupdf/pymupdf) 与 [Shapely](https://github.com/shapely/shapely) 的确定性计算几何算法还原拓扑，严禁调用视觉大模型（VLM）猜测连接关系。
   * 针对字体 `ToUnicode` 映射损坏（乱码）或扫描图像区域，仅允许对局部 Bounding Box 裁剪后调用 [PaddleOCR](https://github.com/PADDLEPADDLE/PADDLEOCR) 兜底，且必须标记 `source_modality="ocr_fallback"`。
   * 云端大模型（GPT / Claude / Gemini）在全系统中处于**纯只读下游（Read-Only Downstream）**，不得直接生成或篡改物理连接图（Physical Graph）与梯形图语法树（Ladder AST）。
5. **开源协议与技术选型基线**：本项目为内部自用且兼容开源发布，底层 PDF 解析直接锁定 **[PyMuPDF (GNU AGPLv3)](https://github.com/pymupdf/pymupdf)** 作为主力实现，同时保留 `PdfBackend` 抽象协议层以便未来扩展。

## 2. EIP V2 六层系统架构基线

系统采用 Python 模块化单体架构（Modular Monolith），自下而上分为六个严格单向依赖的层级：

| 层级 | 模块名称 | 核心技术栈 | 输入数据 | 输出数据与职责边界 |
| :--- | :--- | :--- | :--- | :--- |
| **L0** | **Ingestion（文档摄取与版本层）** | Python `hashlib`, PostgreSQL | PDF / PNG / JPG / GX Works3 标签 CSV | 计算 SHA-256 指纹，只读归档原始文件，注册不可变文档版本。 |
| **L1** | **PDF Modality Profiler（模态探针与标定）** | [PyMuPDF (`fitz`)](https://pymupdf.readthedocs.io/en/latest/page.html) | 原始 PDF 页面 | 判定 `Vector / Raster / Hybrid` 模态；识别乱码文本块；计算页面特征尺度（触点间距 $W_{\text{contact}}$、线宽 $W_{\text{stroke}}$）与自适应几何容差。 |
| **L2-A** | **Vector Geometry（矢量几何提取与缝合）** | [PyMuPDF](https://github.com/pymupdf/pymupdf), [Shapely 2.x (`STRtree`)](https://shapely.readthedocs.io/en/latest/) | 矢量/混合 PDF 页面 | 提取线段、矩形、Bezier 曲线与文本 Span；执行共线缝合、自适应端点吸附、符号几何指纹匹配；输出带审计轨迹的 `GeometryPrimitives`。 |
| **L2-B** | **Raster Perception（光栅感知兜底）** | OpenCV, [PaddleOCR](https://github.com/PADDLEPADDLE/PADDLEOCR) | 扫描页或乱码局部裁剪 ROI | 仅对 L1 标记的乱码文本框或光栅图像区域执行 OCR 与符号候选检测，结果强制带置信度分值。 |
| **L3-A** | **Ladder Compiler（梯形图领域编译器）** | [NetworkX](https://networkx.org/), FX5U Device Profile | L2 图元 + FX5U 寻址与指令配置 | 构建二维网格连接图（`Geometry Graph`）→ 还原含 `MPS/MRD/MPP` 栈语义的指令序列（`Instruction Sequence`）→ 编译为受控执行语句树（`GuardedStatement AST`）。 |
| **L3-B** | **Electrical Compiler（电气原理图编译器）** | [NetworkX](https://networkx.org/), [Shapely](https://github.com/shapely/shapely) | L2 图元 + I/O 分配表 | 提取元器件、端子排、T 型交点与实心连接黑点，构建物理等电位网络（`Physical Netlist`）及两阶段跨页候选链接。 |
| **L4** | **Unified Engineering IR（双层统一工程 IR）** | Pydantic v2, PostgreSQL (JSONB), [NetworkX](https://github.com/networkx/networkx) | L3-A 与 L3-B 编译产物 | 严格隔离并桥接 `Physical Graph` 与 `Ladder Logic IR`，维护“不可变解析快照（Snapshot）+ 人工修订增量补丁（Revision Patch）”。 |
| **L5** | **Validation & Review（形式化校验与审核台）** | [Z3 Theorem Prover (`z3-solver`)](https://github.com/z3prover/z3), SymPy, [PySide6](https://doc.qt.io/qtforpython-6/licenses.html) | L4 统一工程 IR | 执行软元件地址合规校验、悬空支路拦截、双线圈检测、组合逻辑 Z3 等价性校验，并在 PySide6 双视图界面供工程师审核确认。 |
| **L6** | **AI Semantic Copilot（只读语义副驾驶）** | OpenAI / Anthropic / Gemini Structured Outputs | 经 L5 校验通过的只读 IR JSON | 基于携带 `EvidenceAnchor` 的结构化 IR 生成逻辑注释、故障诊断树与维护文档，严禁无证据引用输出。 |

## 3. 核心数据契约（Pydantic v2 Schema 冻结定义）

所有 Agent 在编写任何解析与编译代码前，必须首先在 `src/eip/ir/schema.py` 中实现并遵守以下数据契约：

```python
from __future__ import annotations
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class SourceModality(str, Enum):
    VECTOR = "vector"
    OCR_FALLBACK = "ocr_fallback"
    HYBRID = "hybrid"
    CSV_IMPORT = "csv_import"
    MANUAL_OVERRIDE = "manual_override"


class BoundingBox(BaseModel):
    """PDF 原生未缩放 Point 坐标系 (左上角原点, 1pt = 1/72 inch)"""
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
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)


class EvidenceAnchor(BaseModel):
    """强制绑定到每一个工程实体的原图证据锚点"""
    doc_sha256: str
    page_no: int = Field(..., ge=1)
    bbox: BoundingBox
    source_modality: SourceModality
    confidence: float = Field(..., ge=0.0, le=1.0)
    raw_primitive_ids: list[str] = Field(default_factory=list)


class GeometryMergeRecord(BaseModel):
    """几何端点吸附与共线缝合审计记录（禁止静默合并）"""
    merge_id: str
    page_no: int
    primitive_a_id: str
    primitive_b_id: str
    point_a: tuple[float, float]
    point_b: tuple[float, float]
    euclidean_distance_pt: float
    applied_tolerance_pt: float
    rule_name: Literal["collinear_stitch", "endpoint_snap", "t_junction_snap"]


class TypedDeviceIR(BaseModel):
    """三菱 FX5U 结构化软元件地址表示"""
    raw_text: str
    device_type: str                 # 如 "X", "Y", "M", "D", "T", "C", "SM", "SD"
    base_address: str                # 八进制字符串 (如 "017") 或十进制字符串 (如 "100")
    radix: Literal[8, 10, 16]
    bit_index: Optional[int] = None  # 字元件位指定，如 D100.A -> bit_index=10 (0~15)
    digit_k: Optional[int] = None    # 位组合字指定，如 K4M100 -> digit_k=4 (16-bit)
    index_reg: Optional[str] = None  # 变址寄存器，如 D100Z0 -> index_reg="Z0"
    data_type: Literal["BOOL", "BIT_ARRAY", "WORD", "DWORD", "TIMER", "COUNTER"]
    cpu_profile: str = "FX5U"
    is_valid: bool = True
    validation_error: Optional[str] = None


class LadderSymbolNode(BaseModel):
    """Geometry Graph 中的梯形图基础图元符号节点"""
    symbol_id: str
    symbol_kind: Literal[
        "NO_CONTACT", "NC_CONTACT", "RISE_CONTACT", "FALL_CONTACT",
        "OUT_COIL", "SET_COIL", "RST_COIL", "FUNC_BLOCK", "INV_LINE"
    ]
    mnemonic: str                    # 如 "LD", "AND", "OR", "ANI", "OUT", "MOV", "CMP"
    device: Optional[TypedDeviceIR] = None
    operands: list[TypedDeviceIR | str] = Field(default_factory=list)
    comment_text: Optional[str] = None
    left_port_coord: tuple[float, float]
    right_port_coord: tuple[float, float]
    anchor: EvidenceAnchor


class InstructionStep(BaseModel):
    """还原出的带栈操作语义的梯形图指令序列项"""
    step_no: int
    opcode: str                      # 包含 "MPS", "MRD", "MPP", "LD", "AND", "OR", "OUT", "MOV" 等
    operands: list[str] = Field(default_factory=list)
    source_symbol_id: Optional[str] = None
    stack_depth_after: int = Field(0, ge=0, le=11)


class LogicExprNode(BaseModel):
    """纯布尔与比较组合条件语法树（用于使能条件表达）"""
    expr_type: Literal["ATOM", "NOT", "AND", "OR", "RISE_EDGE", "FALL_EDGE", "COMPARE"]
    operator: Optional[str] = None
    device_ref: Optional[TypedDeviceIR] = None
    children: list[LogicExprNode] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)


class GuardedStatement(BaseModel):
    """受控执行语句：将含 MPS/MRD/MPP 的复杂多输出网络解耦为保序的 (使能条件 -> 执行动作) 列表"""
    exec_order: int                  # 严格对应 PLC 扫描周期内的同网络从上到下执行顺序
    enable_condition: LogicExprNode  # 从左母线经 MPS/MRD/MPP 栈传递到当前输出支路的完整使能布尔树
    action_kind: Literal["COIL_OUT", "COIL_SET", "COIL_RST", "FUNC_EXEC"]
    mnemonic: str                    # 如 "OUT", "SET", "RST", "MOV", "BMOV", "TON"
    target_device: Optional[TypedDeviceIR] = None
    operands: list[TypedDeviceIR | str] = Field(default_factory=list)
    is_stateful: bool = False        # 若涉及 SET/RST/TON/CNT/自修改寄存器，标记为 True
    anchor: EvidenceAnchor


class LadderRungIR(BaseModel):
    """单个梯形图网络（Rung）的完整三层 IR 表达"""
    rung_id: str
    rung_index: int
    step_range: Optional[tuple[int, int]] = None
    rung_comment: Optional[str] = None
    geometry_nodes: list[LadderSymbolNode]
    geometry_edges: list[tuple[str, str]]          # (from_node_or_junction, to_node_or_junction)
    merge_audit_logs: list[GeometryMergeRecord]
    instruction_sequence: list[InstructionStep]    # 含 MPS/MRD/MPP 的显式指令流
    guarded_statements: list[GuardedStatement]     # 供 Z3 与状态分析使用的受控执行语句列表
    verification_status: Literal["VERIFIED", "WARNING", "FAILED", "PENDING_HUMAN"] = "PENDING_HUMAN"
    diagnostics: list[str] = Field(default_factory=list)
    anchor: EvidenceAnchor
```

## 4. 核心确定性算法规范

### 4.1 页面特征尺度标定与自适应几何容差（Adaptive Tolerance）

严禁在代码中硬编码 `1.5pt` 作为全局端点吸附阈值。`L1 PDF Modality Profiler` 必须按以下算法动态标定每页的几何容差：

1. **计算特征基准尺度**：
   * 扫描页面内所有垂直短线段，成对匹配高度相近（$\Delta h / h \le 0.15$）、$Y$ 中心对齐（$\Delta y \le 1.0\text{pt}$）且水平间距在 `2.5pt ～ 15.0pt` 之间的平行竖线对，取其水平间距的中位数作为当前页的**基准触点宽度 $W_{\text{contact}}$**。
   * 取页面所有水平长线段线宽（`width`）的中位数作为**基准线宽 $W_{\text{stroke}}$**（若 PDF 中线条为 `width=0` 的发丝线，则默认设 $W_{\text{stroke}} = 0.5\text{pt}$）。
2. **动态容差计算公式**：
   * **共线微间隙缝合容差**：$\epsilon_{\text{collinear}} = \max(1.5 \times W_{\text{stroke}}, 0.12 \times W_{\text{contact}})$
   * **正交 T 型交点与端点吸附容差**：$\epsilon_{\text{snap}} = \max(2.0 \times W_{\text{stroke}}, 0.15 \times W_{\text{contact}})$
   * **触点内部防短路保护阈值**：$\epsilon_{\text{guard}} = 0.65 \times W_{\text{contact}}$（任何两个属于同一触点左右竖线的端点，或距离在 $[0.65 W_{\text{contact}}, 1.35 W_{\text{contact}}]$ 之间的平行竖线，**绝对禁止合并**）。
3. **审计记录**：每次调用 [Shapely `STRtree`](https://shapely.readthedocs.io/en/latest/) 合并两个距离大于 `0.01pt` 的端点时，必须实例化一条 `GeometryMergeRecord` 并存入当前 Rung 的 `merge_audit_logs`。

### 4.2 梯形图三阶段编译与 `MPS / MRD / MPP` 栈消解算法

针对三菱 FX5U 梯形图（参考 [MELSEC iQ-F FX5 Programming Manual](https://www.mitsubishielectric.com/dl/fa/document/manual/plcf/jy997d55801/jy997d55801z.pdf)），编译器必须严格按以下三步将二维几何网格编译为 `LadderRungIR`：

1. **阶段一：构建有向几何网格图（Geometry Graph）**
   * 识别左母线 $X_{\text{left}}$ 与右母线 $X_{\text{right}}$，切分出单个 Rung 的水平带状区域。
   * 识别区域内的所有符号（常开 `| |`、常闭 `|/|'`、上升沿 `|↑|`、下降沿 `|↓|`、反相线 `|/|'`、线圈 `( )`、功能指令框 `[MOV ...]`）。
   * 将符号从导线集合中“挖除”，暴露出每个符号的左端口 $P_{\text{in}}$ 与右端口 $P_{\text{out}}$。
   * 使用 $\epsilon_{\text{snap}}$ 对剩余水平与垂直导线聚类，每个连通导线簇生成图中的一个**电气连接点（Junction Node $J_k$）**，每个符号作为一条有向边 $e = (J_{\text{left}}, J_{\text{right}})$ 加入 `NetworkX MultiDiGraph`。
2. **阶段二：识别汇合并联（OR）与分支输出（MPS/MRD/MPP）**
   * **输入并联归约（Convergent Parallel Reduction）**：若从节点 $J_a$ 分出的多条支路在右侧某节点 $J_b$ **重新汇合**（即均通往同一个下游中间节点而非各自独立连接到右母线），则将该子图归约为 `LogicExprNode(expr_type="OR", children=[...])`。
   * **输出分流栈生成（Divergent Stack Forking）**：若某连接点 $J_{\text{fork}}$ 向右侧分出 $m \ge 2$ 条通往不同输出端（线圈或功能块）的独立水平/阶梯支路（且在抵达右母线前不再汇合）：
     * 按 $Y$ 坐标从上到下排序这 $m$ 条输出支路 $B_1, B_2, \dots, B_m$；
     * 在进入 $B_1$ 前压入指令 `MPS`（将从左母线到 $J_{\text{fork}}$ 的累积布尔表达式 $E_{\text{prefix}}$ 压入虚拟运算栈 `ResultStack`）；
     * 在进入 $B_2 \dots B_{m-1}$ 前生成指令 `MRD`（读取栈顶 $E_{\text{prefix}}$ 作为该支路的起始使能条件）；
     * 在进入最后一条支路 $B_m$ 前生成指令 `MPP`（弹出栈顶 $E_{\text{prefix}}$ 作为 $B_m$ 的起始使能条件）。
     * 若嵌套层数超过 FX5U 硬件上限（11 层），触发静态语法告警。
3. **阶段三：生成受控执行语句列表（Guarded Statements）与 Z3 校验**
   * 对每个输出支路 $B_i$，将其栈顶前缀条件 $E_{\text{prefix}}$ 与支路自身的局部串联/并联条件 $E_{\text{local}}$ 组合为 `AND(E_prefix, E_local)`，生成按扫描顺序排列的 `GuardedStatement`。
   * **Z3 校验边界规定**：
     * 对 `action_kind == "COIL_OUT"` 的纯组合输出，直接将 `enable_condition` 翻译为 [Z3 `BoolRef`](https://github.com/z3prover/z3) 表达式，与参考指令表（GT-B）或专家表达式（GT-C）构造不等式 `Solver.add(Expr_PDF != Expr_GT)`，验证结果是否为 `unsat`。
     * 对包含 `SET / RST`、定时器 `T`、计数器 `C`、边沿触发或寄存器读写的语句，校验器验证**每个动作的触发使能条件（Trigger Condition）等价性**以及**指令执行序列（Instruction Order & Operands）完全一致**，不将其强行压平为无状态布尔公式。

### 4.3 三菱 FX5U 专用 `Device Profile` 寻址规范

在 `src/eip/plc/profiles/mitsubishi/fx5u/` 下建立 YAML 驱动的寻址与指令规则库，严禁使用单一简陋正则：

| 软元件类别 | 符号前缀 | 进制 (Radix) | 典型合法示例 | 常见非法示例（Lexer 必须拦截） | 复合语法支持要求 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **输入继电器** | `X` | **八进制 (Octal)** | `X0`, `X7`, `X10`, `X17`, `X177` | `X8`, `X9`, `X18`, `X29` | 支持组合位 `K1X0`～`K8X0`、变址 `X0Z0` |
| **输出继电器** | `Y` | **八进制 (Octal)** | `Y0`, `Y7`, `Y10`, `Y17`, `Y177` | `Y8`, `Y9`, `Y18`, `Y99` | 支持组合位 `K1Y0`～`K8Y0`、变址 `Y0Z0` |
| **内部/锁存继电器** | `M`, `L`, `B` | 十进制 (`M/L`) / 十六进制 (`B`) | `M0`, `M8000`, `L100`, `B0FF` | `M-1`, `M10.1`（位元件不可再位指定） | 支持组合位 `K1M0`～`K8M0`、变址 `M100Z0` |
| **特殊继电器/寄存器** | `SM`, `SD` | 十进制 (Decimal) | `SM400`（常ON）, `SM402`, `SD0` | `SM8000`（FX3U 旧地址需提示兼容模式） | 标记只读系统标志位属性 |
| **数据寄存器** | `D`, `W`, `R` | 十进制 (`D/R`) / 十六进制 (`W`) | `D0`, `D100.0`, `D100.A`, `D100.F` | `D100.G`, `D100.16`（位指定仅限十六进制 `0~F`） | 支持字内位指定 `D.b`、变址 `D100Z0` |
| **定时器 / 计数器** | `T`, `ST`, `C`, `LC` | 十进制 (Decimal) | `T0`, `ST10`, `C0`, `LC20` | `T0.5` | 区分触点引用（BOOL）与当前值引用（WORD） |
| **变址寄存器** | `Z`, `V` | 十进制 (Decimal) | `Z0`～`Z19`, `V0`～`V19` | `Z99` | 作为其他软元件的偏移后缀解析 |
| **常数文字** | `K`, `H`, `E` | 十进制 (`K`) / 十六进制 (`H`) / 浮点 (`E`) | `K100`, `K-50`, `H0FF0`, `E1.25` | `K1A`, `H0G` | 作为应用指令操作数解析 |

## 5. 代码仓库目录结构与 Auto-PM Worktree 治理规范

为配合 Auto-PM 的多 Agent 并行开发与 PR 门禁，代码仓库统一采用以下模块化结构，每个 Phase 0 任务绑定专属目录与测试文件，杜绝跨分支合并冲突：

```text
eip/
├── pyproject.toml
├── docs/
│   └── EIP_V2_ARCHITECTURE_AND_PHASE0_SPEC.md
├── fixtures/
│   ├── pdf_samples/                 # 真实 GX Works3 导出 PDF 样本
│   └── ground_truth/                # GT-A (几何 JSON), GT-B (CSV/IL), GT-C (专家逻辑)
├── src/
│   └── eip/
│       ├── ir/
│       │   └── schema.py            # [冻结基线] 第 3 节定义的 Pydantic v2 核心数据契约
│       ├── ingest/
│       │   ├── pdf_backend.py       # [P0-01] PyMuPDFBackend 实现与 PdfBackend 协议
│       │   └── profiler.py          # [P0-01] 模态判定、乱码检测与自适应尺度标定
│       ├── plc/
│       │   ├── profiles/mitsubishi/fx5u/
│       │   │   ├── devices.yaml     # [P0-02] FX5U 软元件规则配置
│       │   │   └── instructions.yaml# [P0-02] FX5U 基础与应用指令签名配置
│       │   └── lexer.py             # [P0-02] DeviceParser 与复合寻址校验器
│       └── ladder/
│           ├── geometry_builder.py  # [P0-03] 符号识别、STRtree 缝合与 Geometry Graph 构建
│           ├── stack_compiler.py    # [P0-04] MPS/MRD/MPP 栈还原与 GuardedStatement 编译
│           └── z3_verifier.py       # [P0-04] Z3 组合逻辑等价性与反例生成校验器
└── tests/
    ├── test_p0_01_pdf_dump.py
    ├── test_p0_02_fx5u_lexer.py
    ├── test_p0_03_geometry_graph.py
    └── test_p0_04_ast_z3.py
```

## 6. Phase 0（5 个工作日）Worktree 任务卡与硬核验收门禁

所有开发 Agent 必须按以下 4 个任务卡创建独立分支与 Worktree。**只有当 `pytest` 验收标准 100% 通过且输出三项实测产物后，才允许合并进入 `main` 分支**。

| 任务 ID | 分支名称 | 依赖项 | 核心交付代码文件 | 可自动化执行的验收标准（PR Merge Gate） |
| :--- | :--- | :--- | :--- | :--- |
| **P0-01** | `feat/p0-01-pdf-vector-dump` | `ir/schema.py` | `src/eip/ingest/pdf_backend.py`<br>`src/eip/ingest/profiler.py`<br>`tests/test_p0_01_pdf_dump.py` | 1. 针对真实 GX Works3 PDF 样本，100% 提取所有线条、矩形、Bezier 曲线与文本 Span 并输出 JSON；<br>2. 自动计算出页面 $W_{\text{contact}}$、$W_{\text{stroke}}$ 与自适应容差 $\epsilon_{\text{snap}}$；<br>3. 准确检测 `\ufffd` 或无 `ToUnicode` 乱码区域；<br>4. 生成将提取出的矢量图元以半透明高亮色叠加回原图的视觉校验 PNG。 |
| **P0-02** | `feat/p0-02-fx5u-device-lexer` | `ir/schema.py` | `src/eip/plc/profiles/mitsubishi/fx5u/*.yaml`<br>`src/eip/plc/lexer.py`<br>`tests/test_p0_02_fx5u_lexer.py` | 1. 完整支持八进制 `X/Y`、十进制 `M/D/T/C/SM/SD`、十六进制 `B/W`；<br>2. 准确解析字元件位访问（`D100.0`～`D100.F`）、位元件组合访问（`K1X0`～`K8M0`）及变址修饰（`D100Z0`）；<br>3. `pytest` 包含不少于 60 个正反边界测试（100% 拦截 `X8`、`Y19`、`D100.G`、`M0.1` 等非法地址）。 |
| **P0-03** | `feat/p0-03-geometry-graph` | P0-01, P0-02 | `src/eip/ladder/geometry_builder.py`<br>`tests/test_p0_03_geometry_graph.py` | 1. 从 P0-01 的矢量图元中识别左右母线、Rung 边界、常开/常闭/边沿触点、线圈与指令框，准确绑定上方软元件地址与下方注释；<br>2. 使用自适应容差 $\epsilon_{\text{snap}}$ 与 `Shapely STRtree` 聚合连通导线节点，输出完整的 `NetworkX` 有向 `Geometry Graph` 及 `GeometryMergeRecord` 审计表；<br>3. 生成含节点编号和边方向的拓扑调试 SVG/PNG，在测试网络上实现零漏检、零内部触点误吸短路。 |
| **P0-04** | `feat/p0-04-ast-roundtrip` | P0-02, P0-03 | `src/eip/ladder/stack_compiler.py`<br>`src/eip/ladder/z3_verifier.py`<br>`tests/test_p0_04_ast_z3.py` | 1. 正确处理输入并联汇合（`OR`）与输出分支栈（`MPS/MRD/MPP`），将 `Geometry Graph` 编译为 `Instruction Sequence` 与 `GuardedStatement` 列表；<br>2. 对比人工标准答案（GT-A/GT-C），5 个典型复杂测试网络的 `GuardedStatement` 结构 100% 一致；<br>3. 注入人工变异测试（Mutation Test：将任一常开改为常闭，或删去一条并联支路），`z3_verifier.py` 必须 100% 捕获逻辑不等价并打印具体输入反例（如 `X0=True, X1=False`）。 |

## 7. 给 Antigravity / Claude Code 的一键启动指令模板

将本文档保存至项目仓库的 `docs/EIP_V2_ARCHITECTURE_AND_PHASE0_SPEC.md` 后，可直接复制以下指令发送给 Antigravity 启动开发：

> 请先完整阅读 `docs/EIP_V2_ARCHITECTURE_AND_PHASE0_SPEC.md`，将其作为 EIP 项目的唯一架构基线。
> 当前严格处于 **Phase 0（技术可行性实测阶段）**，禁止开发任何 Phase 1 的 UI 或云端 AI 模块。
> 请按照规范第 3、5、6 节的要求，首先在主干初始化项目结构与 `src/eip/ir/schema.py` 数据契约，随后按顺序（或分 Worktree）执行 **P0-01、P0-02、P0-03、P0-04** 四个任务，确保每个任务均附带完整可运行的 `pytest` 测试用例并 100% 通过门禁。
