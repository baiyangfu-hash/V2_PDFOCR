from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

# Ensure src/ is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from eip.ingest.pdf_backend import get_default_pdf_backend
from eip.ingest.profiler import PDFModalityProfiler
from eip.ingest.synthetic_gx3_pdf import SyntheticGXWorks3PdfGenerator
from eip.ir.schema import LogicExprNode, TypedDeviceIR
from eip.ladder.geometry_builder import LadderGeometryBuilder
from eip.ladder.stack_compiler import LadderStackCompiler
from eip.ladder.visualizer import render_verification_overlay
from eip.ladder.z3_verifier import LadderFormalVerifier
from eip.plc.lexer import FX5UDeviceLexer


class TestP002FX5UDeviceLexer(unittest.TestCase):
    """P0-02 Merge Gate: 60+ positive and negative boundary tests for FX5U addressing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lexer = FX5UDeviceLexer()

    def test_valid_octal_xy_devices(self) -> None:
        valid_xy = ["X0", "X7", "X10", "X17", "X000", "X177", "Y0", "Y7", "Y10", "Y17", "Y1777"]
        for tok in valid_xy:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertTrue(ir.is_valid, f"Expected {tok} to be valid, got {ir.validation_error}")
                self.assertEqual(ir.radix, 8)
                self.assertEqual(ir.data_type, "BOOL")

    def test_invalid_octal_xy_devices_rejected(self) -> None:
        invalid_xy = ["X8", "X9", "X18", "X19", "X28", "Y8", "Y9", "Y18", "Y99", "X2000", "X0.1"]
        for tok in invalid_xy:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertFalse(ir.is_valid, f"Expected non-octal or out-of-range {tok} to be rejected!")

    def test_valid_decimal_and_hex_devices(self) -> None:
        valid_cases = [
            ("M0", 10, "BOOL"),
            ("M8000", 10, "BOOL"),
            ("L100", 10, "BOOL"),
            ("SM400", 10, "BOOL"),
            ("SM402", 10, "BOOL"),
            ("B0", 16, "BOOL"),
            ("B0FF", 16, "BOOL"),
            ("B1A0", 16, "BOOL"),
            ("D0", 10, "WORD"),
            ("D100", 10, "WORD"),
            ("D7999", 10, "WORD"),
            ("R1000", 10, "WORD"),
            ("W0FF", 16, "WORD"),
            ("SD200", 10, "WORD"),
            ("T0", 10, "TIMER"),
            ("ST10", 10, "TIMER"),
            ("C0", 10, "COUNTER"),
            ("LC20", 10, "COUNTER"),
            ("Z0", 10, "WORD"),
            ("Z19", 10, "WORD"),
            ("V0", 10, "DWORD"),
        ]
        for tok, expected_radix, expected_dtype in valid_cases:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertTrue(ir.is_valid, f"Expected {tok} valid: {ir.validation_error}")
                self.assertEqual(ir.radix, expected_radix)
                self.assertEqual(ir.data_type, expected_dtype)

    def test_word_bit_specification_hex_0_to_f(self) -> None:
        valid_bits = ["D100.0", "D100.9", "D100.A", "D100.B", "D100.F", "W10.C", "R0.F"]
        for tok in valid_bits:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertTrue(ir.is_valid, f"Expected {tok} valid: {ir.validation_error}")
                self.assertEqual(ir.data_type, "BOOL")
                self.assertIsNotNone(ir.bit_index)

        invalid_bits = ["D100.G", "D100.10", "D100.16", "M10.0", "Y0.1", "T0.0"]
        for tok in invalid_bits:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertFalse(ir.is_valid, f"Expected invalid bit spec {tok} to be rejected!")

    def test_digit_specification_and_index_registers(self) -> None:
        valid_comp = [
            ("K1X0", 1, None, "WORD"),
            ("K2Y10", 2, None, "WORD"),
            ("K4M100", 4, None, "WORD"),
            ("K8M0", 8, None, "DWORD"),
            ("D100Z0", None, "Z0", "WORD"),
            ("M10V1", None, "V1", "BOOL"),
            ("X0Z19", None, "Z19", "BOOL"),
            ("K4X0Z0", 4, "Z0", "WORD"),
        ]
        for tok, expected_k, expected_idx, expected_dt in valid_comp:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertTrue(ir.is_valid, f"Expected {tok} valid: {ir.validation_error}")
                self.assertEqual(ir.digit_k, expected_k)
                self.assertEqual(ir.index_reg, expected_idx)
                self.assertEqual(ir.data_type, expected_dt)

        invalid_comp = ["K0X0", "K9M0", "K4D100", "D100Z20", "SM400Z0", "K4D100.0"]
        for tok in invalid_comp:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertFalse(ir.is_valid, f"Expected {tok} to be rejected!")

    def test_constants_k_h_e(self) -> None:
        for tok in ["K0", "K100", "K-50", "H0", "H0FF0", "HFFFF", "E1.25", "E-0.5"]:
            with self.subTest(token=tok):
                ir = self.lexer.parse(tok)
                self.assertTrue(ir.is_valid)
                self.assertTrue(ir.is_constant)
        for bad_tok in ["K12A", "H00G", "E1.2.3"]:
            with self.subTest(token=bad_tok):
                ir = self.lexer.parse(bad_tok)
                self.assertFalse(ir.is_valid)


class TestP0EndToEndPipeline(unittest.TestCase):
    """P0-01, P0-03, P0-04 End-to-End Verification against Ground Truth."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures_dir = REPO_ROOT / "fixtures"
        cls.pdf_path = cls.fixtures_dir / "pdf_samples" / "fx5u_phase0_sample.pdf"
        SyntheticGXWorks3PdfGenerator(cls.pdf_path).generate()

        cls.backend = get_default_pdf_backend()
        cls.prims = cls.backend.extract_page_primitives(cls.pdf_path, page_no=1)
        cls.profiler = PDFModalityProfiler()
        cls.profile = cls.profiler.profile_page(cls.prims)

        cls.geo_builder = LadderGeometryBuilder()
        cls.rungs = cls.geo_builder.build_page_rungs(cls.prims, cls.profile)

        cls.compiler = LadderStackCompiler()
        for r in cls.rungs:
            cls.compiler.compile_rung(r)

        cls.verifier = LadderFormalVerifier()
        cls.diagnostics = cls.verifier.run_static_rules(cls.rungs)

    def test_p0_01_vector_extraction_and_adaptive_scale(self) -> None:
        # Must extract lines, curves, rects, and text spans
        self.assertGreater(len(self.prims.lines), 30)
        self.assertGreaterEqual(len(self.prims.curves), 6)  # 3 coils * 2 bezier arcs
        self.assertEqual(len(self.prims.rects), 2)          # [SET M10] and [MOV K100 D0]

        # Must detect the intentional broken ToUnicode glyph (\ufffd)
        self.assertGreaterEqual(self.profile.garbled_span_count, 1)
        self.assertEqual(self.profile.modality, "HYBRID")

        # Characteristic contact width must match 6.0pt (+/- 0.2pt)
        self.assertAlmostEqual(self.profile.characteristic_contact_width_pt, 6.0, delta=0.25)
        # Adaptive snap tolerance must be > 0.35pt (to stitch the micro-gap) and < eps_guard
        self.assertGreater(self.profile.tolerance_snap_pt, 0.5)
        self.assertLess(
            self.profile.tolerance_snap_pt,
            self.profile.tolerance_symbol_guard_pt,
        )

    def test_p0_03_geometry_graph_and_microgap_audit_log(self) -> None:
        self.assertEqual(len(self.rungs), 3)
        self.assertEqual(len(self.diagnostics), 0, f"Unexpected static errors: {self.diagnostics}")

        # Rung 0: must have recorded the 0.35pt collinear stitch in merge_audit_logs
        r0 = self.rungs[0]
        collinear_merges = [
            m for m in r0.merge_audit_logs if 0.25 <= m.euclidean_distance_pt <= 0.45
        ]
        self.assertGreaterEqual(
            len(collinear_merges),
            1,
            f"Expected 0.35pt micro-gap stitch in audit log, got {r0.merge_audit_logs}",
        )

        # Ensure no contact bars were accidentally shorted together
        self.assertEqual(len(r0.geometry_nodes), 4)  # X0, Y0(seal), X1(NC), Y0(coil)

    def test_p0_04_mps_mrd_mpp_compilation_and_formal_equivalence(self) -> None:
        lexer = FX5UDeviceLexer()

        def atom(dev: str) -> LogicExprNode:
            return LogicExprNode(expr_type="ATOM", operator=dev, device_ref=lexer.parse(dev))

        def neg(dev: str) -> LogicExprNode:
            return LogicExprNode(expr_type="NOT", children=[atom(dev)])

        # ---------------------------------------------------------------------
        # Verify Rung 0: (X0 OR Y0) AND NOT(X1) -> OUT Y0
        # ---------------------------------------------------------------------
        r0 = self.rungs[0]
        self.assertEqual(len(r0.guarded_statements), 1)
        stmt_r0 = r0.guarded_statements[0]
        self.assertEqual(stmt_r0.mnemonic, "OUT")
        self.assertEqual(stmt_r0.target_device.raw_text, "Y0")

        gt_r0 = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(expr_type="OR", children=[atom("X0"), atom("Y0")]),
                neg("X1"),
            ],
        )
        eq_r0 = self.verifier.check_enable_equivalence(stmt_r0.enable_condition, gt_r0)
        self.assertTrue(eq_r0.is_equivalent, eq_r0.details)

        # ---------------------------------------------------------------------
        # Verify Rung 1: MPS / MRD / MPP 3-branch output rung
        # ---------------------------------------------------------------------
        r1 = self.rungs[1]
        opcodes_r1 = [step.opcode for step in r1.instruction_sequence]
        self.assertIn("MPS", opcodes_r1)
        self.assertIn("MRD", opcodes_r1)
        self.assertIn("MPP", opcodes_r1)
        self.assertLess(
            opcodes_r1.index("MPS"),
            opcodes_r1.index("MRD"),
        )
        self.assertLess(
            opcodes_r1.index("MRD"),
            opcodes_r1.index("MPP"),
        )

        self.assertEqual(len(r1.guarded_statements), 3)
        prefix_gt = LogicExprNode(
            expr_type="AND",
            children=[
                atom("X2"),
                LogicExprNode(expr_type="OR", children=[atom("X3"), atom("M0")]),
            ],
        )

        # Branch 1 (MPS): prefix AND X4 -> OUT Y10
        s1 = r1.guarded_statements[0]
        self.assertEqual(s1.mnemonic, "OUT")
        self.assertEqual(s1.target_device.raw_text, "Y10")
        gt_b1 = LogicExprNode(expr_type="AND", children=[prefix_gt, atom("X4")])
        self.assertTrue(self.verifier.check_enable_equivalence(s1.enable_condition, gt_b1).is_equivalent)

        # Branch 2 (MRD): prefix AND NOT(X5) -> SET M10
        s2 = r1.guarded_statements[1]
        self.assertEqual(s2.mnemonic, "SET")
        self.assertEqual(s2.target_device.raw_text, "M10")
        gt_b2 = LogicExprNode(expr_type="AND", children=[prefix_gt, neg("X5")])
        self.assertTrue(self.verifier.check_enable_equivalence(s2.enable_condition, gt_b2).is_equivalent)

        # Branch 3 (MPP): prefix -> MOV K100 D0
        s3 = r1.guarded_statements[2]
        self.assertEqual(s3.mnemonic, "MOV")
        self.assertEqual([op.raw_text for op in s3.operands], ["K100", "D0"])
        self.assertTrue(self.verifier.check_enable_equivalence(s3.enable_condition, prefix_gt).is_equivalent)

        # ---------------------------------------------------------------------
        # Verify Rung 2: RISE_EDGE(X10) AND D100.A -> OUT Y17
        # ---------------------------------------------------------------------
        r2 = self.rungs[2]
        self.assertEqual(len(r2.guarded_statements), 1)
        s_r2 = r2.guarded_statements[0]
        self.assertEqual(s_r2.target_device.raw_text, "Y17")
        gt_r2 = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(expr_type="RISE_EDGE", device_ref=lexer.parse("X10")),
                atom("D100.A"),
            ],
        )
        self.assertTrue(self.verifier.check_enable_equivalence(s_r2.enable_condition, gt_r2).is_equivalent)

    def test_p0_04_mutation_detection_generates_counterexample(self) -> None:
        """Mutation test: flipping X1 from NC to NO must be caught by SAT/SMT solver with a counterexample."""
        lexer = FX5UDeviceLexer()
        r0_cond = self.rungs[0].guarded_statements[0].enable_condition

        # Mutated expression where X1 is NO instead of NC: (X0 OR Y0) AND X1
        mutated_gt = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(
                    expr_type="OR",
                    children=[
                        LogicExprNode(expr_type="ATOM", operator="X0", device_ref=lexer.parse("X0")),
                        LogicExprNode(expr_type="ATOM", operator="Y0", device_ref=lexer.parse("Y0")),
                    ],
                ),
                LogicExprNode(expr_type="ATOM", operator="X1", device_ref=lexer.parse("X1")),
            ],
        )
        res = self.verifier.check_enable_equivalence(r0_cond, mutated_gt)
        self.assertFalse(res.is_equivalent, "Solver failed to catch NC->NO polarity mutation!")
        self.assertIsNotNone(res.counterexample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
