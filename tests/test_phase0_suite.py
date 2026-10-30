from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from eip.ingest import GX3ProjectTableParser
from eip.ingest.pdf_backend import PdfPlumberBackend, get_default_pdf_backend
from eip.ingest.profiler import PDFModalityProfiler
from eip.ingest.synthetic_gx3_pdf import SyntheticGXWorks3PdfGenerator
from eip.ir.schema import LogicExprNode
from eip.ladder.geometry_builder import LadderGeometryBuilder
from eip.ladder.stack_compiler import LadderStackCompiler
from eip.ladder.z3_verifier import LadderFormalVerifier
from eip.plc.lexer import FX5UDeviceLexer


class TestP002FX5UDeviceLexer(unittest.TestCase):
    """P0-02: Strict unit tests for Mitsubishi MELSEC iQ-F (FX5U) Device Lexer."""

    def setUp(self) -> None:
        self.lexer = FX5UDeviceLexer()

    def test_valid_octal_xy_devices(self) -> None:
        for tok, expected_int in [("X0", 0), ("X7", 7), ("X10", 8), ("X17", 15), ("Y0", 0), ("Y27", 23), ("X1777", 1023)]:
            ir = self.lexer.parse(tok)
            self.assertTrue(ir.is_valid, f"Expected {tok} to be valid: {ir.validation_error}")
            self.assertEqual(ir.radix, 8)
            self.assertEqual(ir.address_int, expected_int)
            self.assertEqual(ir.data_type, "BOOL")

    def test_invalid_octal_xy_devices_rejected(self) -> None:
        for bad_tok in ["X8", "X9", "X18", "X29", "Y8", "Y19", "Y80", "X2000"]:
            ir = self.lexer.parse(bad_tok)
            self.assertFalse(ir.is_valid, f"Expected {bad_tok} to be REJECTED as invalid octal")
            self.assertIsNotNone(ir.validation_error)

    def test_valid_decimal_and_hex_devices(self) -> None:
        for tok, radix, dtype in [
            ("M0", 10, "BOOL"),
            ("M8000", 10, "BOOL"),
            ("SM400", 10, "BOOL"),
            ("F0", 10, "BOOL"),
            ("S20", 10, "BOOL"),
            ("D100", 10, "WORD"),
            ("SD200", 10, "WORD"),
            ("R32767", 10, "WORD"),
            ("B1A", 16, "BOOL"),
            ("SB49", 16, "BOOL"),
            ("W0FF", 16, "WORD"),
            ("SW1A8", 16, "WORD"),
            ("T0", 10, "TIMER"),
            ("ST10", 10, "TIMER"),
            ("C15", 10, "COUNTER"),
            ("LC20", 10, "COUNTER"),
            ("P0", 10, "POINTER"),
            ("T_0[0]", 10, "LABEL"),
            ("准备完成1…", 10, "LABEL"),
        ]:
            ir = self.lexer.parse(tok)
            self.assertTrue(ir.is_valid, f"Expected {tok} to be valid: {ir.validation_error}")
            self.assertEqual(ir.radix, radix)
            self.assertEqual(ir.data_type, dtype)

    def test_word_bit_specification_hex_0_to_f(self) -> None:
        ir_a = self.lexer.parse("D100.A")
        self.assertTrue(ir_a.is_valid)
        self.assertEqual(ir_a.bit_index, 10)
        self.assertEqual(ir_a.data_type, "BOOL")

        ir_0 = self.lexer.parse("D200.0")
        self.assertTrue(ir_0.is_valid)
        self.assertEqual(ir_0.bit_index, 0)

        for bad_bit in ["D100.16", "D100.G", "M10.0", "X0.1"]:
            ir_bad = self.lexer.parse(bad_bit)
            self.assertFalse(ir_bad.is_valid, f"Expected {bad_bit} to be rejected")

    def test_digit_specification_and_index_registers(self) -> None:
        ir_k4 = self.lexer.parse("K4M0")
        self.assertTrue(ir_k4.is_valid)
        self.assertEqual(ir_k4.digit_k, 4)
        self.assertEqual(ir_k4.data_type, "WORD")

        ir_k8f = self.lexer.parse("K8F0")
        self.assertTrue(ir_k8f.is_valid)
        self.assertEqual(ir_k8f.digit_k, 8)
        self.assertEqual(ir_k8f.data_type, "DWORD")

        ir_idx = self.lexer.parse("D100Z0")
        self.assertTrue(ir_idx.is_valid)
        self.assertEqual(ir_idx.index_reg, "Z0")

        for bad_tok in ["K9M0", "K0X0", "K2D100", "D100Z25"]:
            self.assertFalse(self.lexer.parse(bad_tok).is_valid)

    def test_constants_k_h_e(self) -> None:
        k_ir = self.lexer.parse("K-500")
        self.assertTrue(k_ir.is_valid)
        self.assertTrue(k_ir.is_constant)
        self.assertEqual(k_ir.address_int, -500)

        h_ir = self.lexer.parse("H1A2F")
        self.assertTrue(h_ir.is_valid)
        self.assertEqual(h_ir.address_int, 0x1A2F)

        e_ir = self.lexer.parse("E3.1415")
        self.assertTrue(e_ir.is_valid)
        self.assertTrue(e_ir.is_constant)


class TestP0EndToEndPipeline(unittest.TestCase):
    """P0-01, P0-03, P0-04 End-to-End Pipeline Tests on Synthetic Vector Ladder PDF."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.pdf_path = Path(cls.tmp_dir.name) / "fx5u_phase0_sample.pdf"
        SyntheticGXWorks3PdfGenerator(cls.pdf_path).generate()

        cls.backend = get_default_pdf_backend()
        cls.profiler = PDFModalityProfiler()
        cls.builder = LadderGeometryBuilder()
        cls.compiler = LadderStackCompiler()
        cls.verifier = LadderFormalVerifier()

        cls.prims = cls.backend.extract_page_primitives(cls.pdf_path, page_no=1)
        cls.profile = cls.profiler.profile_page(cls.prims)
        cls.rungs = cls.builder.build_page_rungs(cls.prims, cls.profile)
        for r in cls.rungs:
            cls.compiler.compile_rung(r)
        cls.diagnostics = cls.verifier.run_static_rules(cls.rungs)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp_dir.cleanup()

    def test_p0_01_vector_extraction_and_adaptive_scale(self) -> None:
        self.assertGreater(self.profile.vector_line_count, 40)
        self.assertAlmostEqual(self.profile.characteristic_contact_width_pt, 6.0, delta=0.5)
        self.assertGreaterEqual(self.profile.tolerance_snap_pt, 1.0)
        self.assertLess(self.profile.tolerance_snap_pt, self.profile.tolerance_symbol_guard_pt)
        self.assertEqual(self.profile.garbled_span_count, 1)
        self.assertEqual(self.profile.modality, "HYBRID")

    def test_p0_03_geometry_graph_and_microgap_audit_log(self) -> None:
        self.assertEqual(len(self.rungs), 3)
        r0, r1, r2 = self.rungs
        self.assertEqual(len(r0.diagnostics), 0)
        self.assertEqual(len(r1.diagnostics), 0)
        self.assertEqual(len(r2.diagnostics), 0)
        self.assertGreaterEqual(len(r0.merge_audit_logs), 1)
        stitch = r0.merge_audit_logs[0]
        self.assertAlmostEqual(stitch.euclidean_distance_pt, 0.35, delta=0.15)

    def test_p0_04_mps_mrd_mpp_compilation_and_formal_equivalence(self) -> None:
        r0, r1, r2 = self.rungs
        opcodes_r1 = [step.opcode for step in r1.instruction_sequence]
        self.assertIn("MPS", opcodes_r1)
        self.assertIn("MRD", opcodes_r1)
        self.assertIn("MPP", opcodes_r1)
        self.assertEqual(len(r1.guarded_statements), 3)

        lexer = FX5UDeviceLexer()
        gt_r0 = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(
                    expr_type="NOT",
                    children=[LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X1"))],
                ),
                LogicExprNode(
                    expr_type="OR",
                    children=[
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("Y0")),
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X0")),
                    ],
                ),
            ],
        )
        parsed_r0_cond = r0.guarded_statements[0].enable_condition
        eq_r0 = self.verifier.check_enable_equivalence(parsed_r0_cond, gt_r0)
        self.assertTrue(eq_r0.is_equivalent, eq_r0.details)

        gt_r1_b3 = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X2")),
                LogicExprNode(
                    expr_type="OR",
                    children=[
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X3")),
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("M0")),
                    ],
                ),
            ],
        )
        eq_r1_b3 = self.verifier.check_enable_equivalence(
            r1.guarded_statements[2].enable_condition, gt_r1_b3
        )
        self.assertTrue(eq_r1_b3.is_equivalent, eq_r1_b3.details)

    def test_p0_04_mutation_detection_generates_counterexample(self) -> None:
        r0 = self.rungs[0]
        lexer = FX5UDeviceLexer()
        mutated_r0 = LogicExprNode(
            expr_type="AND",
            children=[
                LogicExprNode(
                    expr_type="OR",
                    children=[
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X0")),
                        LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("Y0")),
                    ],
                ),
                LogicExprNode(expr_type="ATOM", device_ref=lexer.parse("X1")),
            ],
        )
        res = self.verifier.check_enable_equivalence(
            r0.guarded_statements[0].enable_condition, mutated_r0
        )
        self.assertFalse(res.is_equivalent)
        self.assertIsNotNone(res.counterexample)


class TestP0RealGXWorks3Project(unittest.TestCase):
    """Real GX Works3 Production PDF Verification Suite (01_梯形图.pdf, 05_软元件注释.pdf, 07_系统参数.pdf)."""

    UPLOAD_DIR = Path("/working_dir/c_ba6c7fb98dadac19/user_uploaded_files/ttl=63d/user_uploaded")

    def test_real_gx3_ladder_pages_compile_with_zero_diagnostics(self) -> None:
        ladder_pdf = self.UPLOAD_DIR / "01_梯形图.pdf"
        if not ladder_pdf.exists():
            self.skipTest("Real GX Works3 PDF not attached in current environment")

        backend = PdfPlumberBackend()
        profiler = PDFModalityProfiler()
        builder = LadderGeometryBuilder()
        compiler = LadderStackCompiler()
        verifier = LadderFormalVerifier()

        expected_rungs_per_page = {1: 6, 2: 4, 7: 5, 10: 3, 14: 4, 51: 4}
        for page_no, expected_rungs in expected_rungs_per_page.items():
            prims = backend.extract_page_primitives(ladder_pdf, page_no=page_no)
            prof = profiler.profile_page(prims)
            self.assertEqual(prof.modality, "VECTOR")
            self.assertAlmostEqual(prof.characteristic_contact_width_pt, 5.82, delta=0.15)
            rungs = builder.build_page_rungs(prims, prof)
            for r in rungs:
                compiler.compile_rung(r)
            diags = verifier.run_static_rules(rungs)
            self.assertEqual(
                len(diags),
                0,
                f"Expected 0 diagnostics on real GX Works3 Page {page_no}, got: {diags}",
            )
            self.assertEqual(
                len(rungs),
                expected_rungs,
                f"Expected {expected_rungs} rungs on Page {page_no}, got {len(rungs)}",
            )

    def test_real_gx3_comment_and_system_param_tables(self) -> None:
        comment_pdf = self.UPLOAD_DIR / "05_软元件注释.pdf"
        sys_pdf = self.UPLOAD_DIR / "07_系统参数.pdf"
        if not comment_pdf.exists() or not sys_pdf.exists():
            self.skipTest("Real GX Works3 project tables not attached")

        parser = GX3ProjectTableParser()
        slots = parser.parse_io_rack_config(sys_pdf)
        self.assertEqual(len(slots), 8)
        self.assertEqual(slots[0].model_name, "FX5U-80MT/ES")
        self.assertEqual(slots[1].model_name, "FX5-CCLIEF")

        comments = parser.parse_device_comments(comment_pdf, max_pages=5)
        self.assertIn("M0", comments)
        self.assertEqual(comments["M0"].comment, "手动")


if __name__ == "__main__":
    unittest.main(verbosity=2)
