from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from eip.ingest import (
    ElectricalSchematicParser,
    SolarFrameTranslationEngine,
    TableQualityVerifier,
    FBDProgramParser,
    GX3ProjectTableParser,
    STStructuredTextParser,
)
from eip.ingest.pdf_backend import PdfPlumberBackend, get_default_pdf_backend
from eip.ingest.profiler import PDFModalityProfiler
from eip.ingest.synthetic_gx3_pdf import SyntheticGXWorks3PdfGenerator
from eip.ir.schema import LogicExprNode
from eip.ladder.geometry_builder import LadderGeometryBuilder
from eip.ladder.stack_compiler import LadderStackCompiler
from eip.ladder.z3_verifier import LadderFormalVerifier, ProjectKnowledgeGraph
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


class TestP1CrossDocumentKnowledgeGraph(unittest.TestCase):
    """Phase 1 Cross-Language (ST + FBD/LD + Ladder) & Cross-Document Verification Suite."""

    UPLOAD_DIR = Path("/working_dir/c_ba6c7fb98dadac19/user_uploaded_files/ttl=63d/user_uploaded")

    def test_st_io_mapping_and_fb_state_machine_parsing(self) -> None:
        st_pdf = self.UPLOAD_DIR / "02_ST.pdf"
        fb_pdf = self.UPLOAD_DIR / "04_FB_FUN.pdf"
        if not st_pdf.exists() or not fb_pdf.exists():
            self.skipTest("ST / FB PDFs not attached")

        st_parser = STStructuredTextParser()
        st_main = st_parser.parse_st_pdf(st_pdf)
        self.assertEqual(st_main.program_name, "分料送料IO映射")
        self.assertEqual(len(st_main.assignments), 24)
        self.assertIn("Y10", st_main.written_symbols)
        self.assertIn("Conveyor[1].输送线正转", st_main.read_symbols)

        fb_ir = st_parser.parse_st_pdf(fb_pdf)
        self.assertEqual(fb_ir.program_name, "送料分料测试")
        self.assertEqual(fb_ir.program_kind, "FB_DEF")
        self.assertEqual(len(fb_ir.timer_calls), 5)
        self.assertEqual(fb_ir.state_steps, [0, 1, 2, 3, 10, 20, 30, 50, 60, 70, 80])

    def test_fbd_multipage_stitching_and_cross_doc_bug_detection(self) -> None:
        fbd_pdf = self.UPLOAD_DIR / "03_FBD_LD.pdf"
        st_pdf = self.UPLOAD_DIR / "02_ST.pdf"
        fb_pdf = self.UPLOAD_DIR / "04_FB_FUN.pdf"
        cclink_pdf = self.UPLOAD_DIR / "09_模块信息.pdf"
        ladder_pdf = self.UPLOAD_DIR / "01_梯形图.pdf"
        if not fbd_pdf.exists():
            self.skipTest("03_FBD_LD.pdf not attached")

        fbd_parser = FBDProgramParser()
        instances, body_signals = fbd_parser.parse_fbd_pdf(fbd_pdf)
        self.assertEqual(len(instances), 4)
        self.assertEqual(
            [inst.instance_name for inst in instances],
            ["送料分料测试_1", "送料分料测试_2", "送料分料测试_3", "送料分料测试_4"],
        )
        self.assertEqual(instances[0].pages, [5, 7])
        self.assertEqual(instances[2].pages, [3, 5])

        st_parser = STStructuredTextParser()
        st_main = st_parser.parse_st_pdf(st_pdf)
        fb_def = st_parser.parse_st_pdf(fb_pdf)
        cclink_rules = GX3ProjectTableParser().parse_cclink_refresh_config(cclink_pdf)
        self.assertEqual(len(cclink_rules), 4)

        backend = PdfPlumberBackend()
        profiler = PDFModalityProfiler()
        builder = LadderGeometryBuilder()
        compiler = LadderStackCompiler()
        prims14 = backend.extract_page_primitives(ladder_pdf, 14)
        prof14 = profiler.profile_page(prims14)
        rungs14 = [compiler.compile_rung(r) for r in builder.build_page_rungs(prims14, prof14)]

        pkg = ProjectKnowledgeGraph()
        pkg.build_from_project(
            st_main=st_main,
            fb_def=fb_def,
            fbd_instances=instances,
            fbd_body_signals=body_signals,
            cclink_rules=cclink_rules,
            ladder_rungs_by_page={14: rungs14},
        )

        trace_y10 = pkg.trace_backward("Y10", max_depth=4)
        upstream_nodes = {edge[0] for edge in trace_y10}
        self.assertIn("Conveyor[1].输送线正转", upstream_nodes)
        self.assertIn("FB_INST:送料分料测试_1", upstream_nodes)
        self.assertIn("X21", upstream_nodes)
        self.assertIn("输送自动启动", upstream_nodes)
        self.assertIn("M4", upstream_nodes)

        xdoc_diags = pkg.run_cross_document_checks()
        rule_ids = [d.rule_id for d in xdoc_diags]
        self.assertIn("XDOC-01", rule_ids)
        self.assertIn("XDOC-02", rule_ids)
        self.assertIn("XDOC-03", rule_ids)


class TestP2ElectricalSchematicReconciliation(unittest.TestCase):
    """Phase 2 Electrical Schematic (5.边框储料机.pdf) & Program-First Reconciliation Suite."""

    UPLOAD_DIR = Path("/working_dir/c_ba6c7fb98dadac19/user_uploaded_files/ttl=63d/user_uploaded")

    def test_schematic_176_channels_and_program_first_reconciliation(self) -> None:
        schem_pdf = self.UPLOAD_DIR / "5.边框储料机.pdf"
        cmt_pdf = self.UPLOAD_DIR / "05_软元件注释.pdf"
        if not schem_pdf.exists() or not cmt_pdf.exists():
            self.skipTest("5.边框储料机.pdf or 05_软元件注释.pdf not attached")

        tbl_parser = GX3ProjectTableParser()
        plc_comments = tbl_parser.parse_device_comments(cmt_pdf)
        schem_parser = ElectricalSchematicParser()
        channels, power_circuits = schem_parser.parse_and_reconcile(
            schem_pdf, plc_comments
        )

        # 22 I/O pages * 8 channels/page = 176 PLC I/O channels (120 X inputs + 56 Y outputs)
        self.assertEqual(len(channels), 176)
        self.assertEqual(len(power_circuits), 8)

        ch_map = {c.canonical_device: c for c in channels}

        # Verify physical relay + solenoid mapping on Y30 and Y43
        self.assertEqual(ch_map["Y30"].component_tags, ["-KA2", "-YV1"])
        self.assertEqual(ch_map["Y43"].component_tags, ["-KA13", "-YV12"])
        self.assertTrue(ch_map["Y43"].has_pin_typo)
        self.assertEqual(ch_map["Y43"].raw_top_pins, ["Y33"])

        # Verify Page 22/23 X61..X67 and X71..X77 pin typos (14 input typos + 1 output typo = 15 total)
        typo_channels = [c.canonical_device for c in channels if c.has_pin_typo]
        self.assertEqual(len(typo_channels), 15)
        self.assertIn("X61", typo_channels)
        self.assertIn("X71", typo_channels)
        self.assertIn("Y43", typo_channels)

        # Verify Program-First Spare Activation (X150..X157, X160..X161, Y45..Y47 = 13 channels)
        activated = [
            c.canonical_device
            for c in channels
            if c.reconciliation_status == "SPARE_ACTIVATED_IN_PLC"
        ]
        self.assertEqual(len(activated), 13)
        self.assertEqual(ch_map["X150"].authoritative_label_cn, "1层边框检测开关1")
        self.assertEqual(ch_map["Y45"].authoritative_label_cn, "组框机暂停")
        self.assertEqual(ch_map["Y47"].authoritative_label_cn, "给打胶机安全区信号")

    def test_end_to_end_physical_valve_to_sensor_causal_trace(self) -> None:
        schem_pdf = self.UPLOAD_DIR / "5.边框储料机.pdf"
        cmt_pdf = self.UPLOAD_DIR / "05_软元件注释.pdf"
        fbd_pdf = self.UPLOAD_DIR / "03_FBD_LD.pdf"
        st_pdf = self.UPLOAD_DIR / "02_ST.pdf"
        fb_pdf = self.UPLOAD_DIR / "04_FB_FUN.pdf"
        cclink_pdf = self.UPLOAD_DIR / "09_模块信息.pdf"
        if not schem_pdf.exists():
            self.skipTest("5.边框储料机.pdf not attached")

        tbl_parser = GX3ProjectTableParser()
        plc_comments = tbl_parser.parse_device_comments(cmt_pdf)
        channels, power_circuits = ElectricalSchematicParser().parse_and_reconcile(
            schem_pdf, plc_comments
        )
        instances, body_signals = FBDProgramParser().parse_fbd_pdf(fbd_pdf)
        st_parser = STStructuredTextParser()
        st_main = st_parser.parse_st_pdf(st_pdf)
        fb_def = st_parser.parse_st_pdf(fb_pdf)
        cclink = tbl_parser.parse_cclink_refresh_config(cclink_pdf)

        pkg = ProjectKnowledgeGraph()
        pkg.build_from_project(
            st_main=st_main,
            fb_def=fb_def,
            fbd_instances=instances,
            fbd_body_signals=body_signals,
            cclink_rules=cclink,
            ladder_rungs_by_page={},
            schematic_channels=channels,
            power_circuits=power_circuits,
        )

        # Trace from physical solenoid valve HW:-YV1 backward to physical proximity sensors HW:-SQP1, HW:-PR10
        trace_yv1 = pkg.trace_backward("HW:-YV1", max_depth=6)
        upstream = {e[0] for e in trace_yv1}
        self.assertIn("HW:-KA2", upstream)
        self.assertIn("Y30", upstream)
        self.assertIn("Conveyor[1].阻挡气缸", upstream)
        self.assertIn("FB_INST:送料分料测试_1", upstream)
        self.assertIn("X21", upstream)
        self.assertIn("HW:-SQP1", upstream)
        self.assertIn("HW:-PR10", upstream)

        diags = pkg.run_cross_document_checks()
        rule_ids = {d.rule_id for d in diags}
        self.assertTrue(
            {"XDOC-01", "XDOC-02", "XDOC-03", "SCHEM-01", "SCHEM-02", "SCHEM-03", "SCHEM-04"}.issubset(
                rule_ids
            )
        )


    def test_bilingual_translation_engine_and_5fold_verifier(self) -> None:
        schem_pdf = self.UPLOAD_DIR / "5.边框储料机.pdf"
        cmt_pdf = self.UPLOAD_DIR / "05_软元件注释.pdf"
        if not schem_pdf.exists() or not cmt_pdf.exists():
            self.skipTest("5.边框储料机.pdf or 05_软元件注释.pdf not attached")

        plc_comments = GX3ProjectTableParser().parse_device_comments(cmt_pdf)
        channels, _ = ElectricalSchematicParser().parse_and_reconcile(
            schem_pdf, plc_comments
        )
        report = TableQualityVerifier.verify_channels(channels)
        self.assertTrue(report["passed"], f"5-fold verification failed: {report}")
        self.assertEqual(report["total_rows"], 176)
        self.assertEqual(report["cjk_residue_count"], 0)

        ch_map = {c.canonical_device: c for c in channels}
        self.assertEqual(ch_map["X21"].standardized_en_desc, "L1 Stopper Pre-Prox SW")
        self.assertEqual(ch_map["X21"].iec_symbolic_tag_en, "DI_X021_SQP1_L1StopperPreProxSW")
        self.assertEqual(ch_map["X102"].standardized_en_desc, "Gluing Mach Pick Done Sig")
        self.assertEqual(ch_map["Y47"].standardized_en_desc, "To Gluing Mach Safe Zone Sig")


if __name__ == "__main__":
    unittest.main(verbosity=2)
