# EIP V2 (`eip-phase0`) — 工业图纸与梯形图编译系统 (Phase 0 实测基线)

本仓库包含 **Electrical Intelligence Platform (EIP V2)** 的架构规范文档与已100%通过自动化测试的 **Phase 0（P0-01 ～ P0-04）** 核心代码实现。

## 1. 目录结构

```text
eip-phase0/
├── pyproject.toml
├── README.md
├── docs/
│   └── EIP_V2_ARCHITECTURE_AND_PHASE0_SPEC.md   # EIP V2 冻结架构规范
├── fixtures/
│   ├── pdf_samples/
│   │   └── fx5u_phase0_sample.pdf               # 合成 GX Works3 矢量梯形图样本
│   └── verification_outputs/
│       ├── p0_visual_verification.png           # 原图 + 识别框 + 缝合点叠加图
│       └── p0_compiled_ir_dump.json             # 完整三层 LadderRungIR 输出
├── src/
│   └── eip/
│       ├── ir/
│       │   └── schema.py                        # 冻结双层 IR 数据契约
│       ├── ingest/
│       │   ├── synthetic_gx3_pdf.py             # GX Works3 矢量梯形图 PDF 合成器
│       │   ├── pdf_backend.py                   # PyMuPDF / pdfplumber 双引擎后端
│       │   └── profiler.py                      # P0-01: 模态检测与自适应容差标定
│       ├── plc/
│       │   ├── profiles/mitsubishi/fx5u/
│       │   │   ├── devices.yaml                 # FX5U 软元件寻址规范
│       │   │   └── instructions.yaml            # FX5U 指令集规范
│       │   └── lexer.py                         # P0-02: FX5U 软元件词法与复合语法解析器
│       └── ladder/
│           ├── geometry_builder.py              # P0-03: Shapely STRtree 拓扑构建与微间隙缝合
│           ├── stack_compiler.py                # P0-04: MPS/MRD/MPP 栈语义与 Guarded AST 编译
│           ├── z3_verifier.py                   # P0-04: Z3 / SymPy-SAT 等价性证明与反例生成
│           └── visualizer.py                    # 视觉叠加调试渲染器
└── tests/
    └── test_phase0_suite.py                     # Phase 0 完整单元测试与端到端回归测试
```

## 2. 快速运行测试

```bash
# 使用标准库 unittest 直接运行
PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*.py" -v

# 或在安装 pytest 后运行
pytest -v
```
