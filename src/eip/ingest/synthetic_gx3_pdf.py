from __future__ import annotations

from pathlib import Path
from typing import Tuple
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas


class SyntheticGXWorks3PdfGenerator:
    """
    Generates a high-fidelity Mitsubishi GX Works3 vector PDF ladder diagram
    using top-down PDF Point coordinates (1pt = 1/72 inch, A4 Landscape: 841.89 x 595.27 pt).
    """

    PAGE_W, PAGE_H = landscape(A4)
    X_LEFT = 70.0
    X_RIGHT = 760.0
    CONTACT_W = 6.0
    CONTACT_H = 14.0
    COIL_W = 14.0
    COIL_H = 14.0
    STROKE_W = 0.6

    def __init__(self, output_path: Path) -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def _ry(self, y_top: float) -> float:
        return float(self.PAGE_H - y_top)

    def _line(self, c: canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
        c.setLineWidth(self.STROKE_W)
        c.line(x0, self._ry(y0), x1, self._ry(y1))

    def _draw_contact(
        self,
        c: canvas.Canvas,
        cx: float,
        cy: float,
        device: str,
        comment: str = "",
        mode: str = "NO",
    ) -> Tuple[float, float]:
        half_w = self.CONTACT_W / 2.0
        half_h = self.CONTACT_H / 2.0
        x_left = cx - half_w
        x_right = cx + half_w

        self._line(c, x_left, cy - half_h, x_left, cy + half_h)
        self._line(c, x_right, cy - half_h, x_right, cy + half_h)

        if mode == "NC":
            self._line(c, x_left - 1.0, cy + half_h - 1.0, x_right + 1.0, cy - half_h + 1.0)
        elif mode == "RISE":
            self._line(c, cx, cy + half_h - 1.0, cx, cy - half_h + 1.0)
            self._line(c, cx - 2.0, cy - half_h + 4.0, cx, cy - half_h + 1.0)
            self._line(c, cx + 2.0, cy - half_h + 4.0, cx, cy - half_h + 1.0)
        elif mode == "FALL":
            self._line(c, cx, cy - half_h + 1.0, cx, cy + half_h - 1.0)
            self._line(c, cx - 2.0, cy + half_h - 4.0, cx, cy + half_h - 1.0)
            self._line(c, cx + 2.0, cy + half_h - 4.0, cx, cy + half_h - 1.0)

        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, self._ry(cy - half_h - 4.0), device)
        if comment:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(cx, self._ry(cy + half_h + 9.0), comment)
        return (x_left, x_right)

    def _draw_coil(
        self,
        c: canvas.Canvas,
        cx: float,
        cy: float,
        device: str,
        comment: str = "",
    ) -> Tuple[float, float]:
        half_w = self.COIL_W / 2.0
        half_h = self.COIL_H / 2.0
        x_left = cx - half_w
        x_right = cx + half_w

        c.setLineWidth(self.STROKE_W)
        c.bezier(
            x_left + 3.0,
            self._ry(cy - half_h),
            x_left - 1.5,
            self._ry(cy - half_h * 0.4),
            x_left - 1.5,
            self._ry(cy + half_h * 0.4),
            x_left + 3.0,
            self._ry(cy + half_h),
        )
        c.bezier(
            x_right - 3.0,
            self._ry(cy - half_h),
            x_right + 1.5,
            self._ry(cy - half_h * 0.4),
            x_right + 1.5,
            self._ry(cy + half_h * 0.4),
            x_right - 3.0,
            self._ry(cy + half_h),
        )

        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, self._ry(cy - half_h - 4.0), device)
        if comment:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(cx, self._ry(cy + half_h + 9.0), comment)
        return (x_left, x_right)

    def _draw_box_instruction(
        self,
        c: canvas.Canvas,
        x0: float,
        cy: float,
        width: float,
        text_inside: str,
        comment: str = "",
    ) -> Tuple[float, float]:
        half_h = 10.0
        y0 = cy - half_h
        y1 = cy + half_h
        x1 = x0 + width
        c.setLineWidth(self.STROKE_W)
        c.rect(x0, self._ry(y1), width, y1 - y0, stroke=1, fill=0)
        c.setFont("Helvetica", 8)
        c.drawCentredString((x0 + x1) / 2.0, self._ry(cy + 2.5), text_inside)
        if comment:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString((x0 + x1) / 2.0, self._ry(y1 + 9.0), comment)
        return (x0, x1)

    def generate(self) -> Path:
        c = canvas.Canvas(str(self.output_path), pagesize=landscape(A4))

        c.setFont("Helvetica-Bold", 10)
        c.drawString(70.0, self._ry(32.0), "GX Works3 Ladder Diagram Export - Project: FX5U_CELL_CTRL")

        self._line(c, self.X_LEFT, 55.0, self.X_LEFT, 525.0)
        self._line(c, self.X_RIGHT, 55.0, self.X_RIGHT, 525.0)

        # RUNG 0: (X0 OR Y0) AND NOT(X1) -> OUT Y0 (with 0.35pt micro-gap at x=275.0)
        y_r0_main = 88.0
        y_r0_sub = 142.0
        c.setFont("Helvetica", 8)
        c.drawString(30.0, self._ry(y_r0_main + 3.0), "0")

        x0_l, x0_r = self._draw_contact(c, 160.0, y_r0_main, "X0", "START_PB", mode="NO")
        x1_l, x1_r = self._draw_contact(c, 330.0, y_r0_main, "X1", "STOP_PB", mode="NC")
        y0_c_l, y0_c_r = self._draw_coil(c, 680.0, y_r0_main, "Y0", "MAIN_MOTOR")
        y0_s_l, y0_s_r = self._draw_contact(c, 160.0, y_r0_sub, "Y0", "SEAL_IN", mode="NO")

        x_par_join = 230.0
        self._line(c, self.X_LEFT, y_r0_main, x0_l, y_r0_main)
        self._line(c, x0_r, y_r0_main, x_par_join, y_r0_main)
        self._line(c, self.X_LEFT, y_r0_sub, y0_s_l, y_r0_sub)
        self._line(c, y0_s_r, y_r0_sub, x_par_join, y_r0_sub)
        self._line(c, x_par_join, y_r0_main, x_par_join, y_r0_sub)

        # Intentional 0.35pt micro-gap
        self._line(c, x_par_join, y_r0_main, 275.0, y_r0_main)
        self._line(c, 275.35, y_r0_main, x1_l, y_r0_main)

        self._line(c, x1_r, y_r0_main, y0_c_l, y_r0_main)
        self._line(c, y0_c_r, y_r0_main, self.X_RIGHT, y_r0_main)

        # RUNG 1: MPS / MRD / MPP Multi-Output Branching Rung
        y_r1_b1 = 225.0
        y_r1_or = 278.0
        y_r1_b2 = 330.0
        y_r1_b3 = 385.0
        c.setFont("Helvetica", 8)
        c.drawString(30.0, self._ry(y_r1_b1 + 3.0), "6")

        x2_l, x2_r = self._draw_contact(c, 135.0, y_r1_b1, "X2", "AUTO_MODE", mode="NO")
        x3_l, x3_r = self._draw_contact(c, 270.0, y_r1_b1, "X3", "PART_OK", mode="NO")
        m0_l, m0_r = self._draw_contact(c, 270.0, y_r1_or, "M0", "BYPASS", mode="NO")

        x_or_left = 210.0
        x_or_right = 335.0
        x_fork = 390.0

        self._line(c, self.X_LEFT, y_r1_b1, x2_l, y_r1_b1)
        self._line(c, x2_r, y_r1_b1, x_or_left, y_r1_b1)
        self._line(c, x_or_left, y_r1_b1, x3_l, y_r1_b1)
        self._line(c, x3_r, y_r1_b1, x_or_right, y_r1_b1)

        self._line(c, x_or_left, y_r1_b1, x_or_left, y_r1_or)
        self._line(c, x_or_left, y_r1_or, m0_l, y_r1_or)
        self._line(c, m0_r, y_r1_or, x_or_right, y_r1_or)
        self._line(c, x_or_right, y_r1_b1, x_or_right, y_r1_or)

        self._line(c, x_or_right, y_r1_b1, x_fork, y_r1_b1)
        self._line(c, x_fork, y_r1_b1, x_fork, y_r1_b3)

        x4_l, x4_r = self._draw_contact(c, 480.0, y_r1_b1, "X4", "CLAMP_SW", mode="NO")
        y10_l, y10_r = self._draw_coil(c, 680.0, y_r1_b1, "Y10", "CLAMP_VALVE")
        self._line(c, x_fork, y_r1_b1, x4_l, y_r1_b1)
        self._line(c, x4_r, y_r1_b1, y10_l, y_r1_b1)
        self._line(c, y10_r, y_r1_b1, self.X_RIGHT, y_r1_b1)

        x5_l, x5_r = self._draw_contact(c, 480.0, y_r1_b2, "X5", "FAULT_IN", mode="NC")
        set_l, set_r = self._draw_box_instruction(c, 635.0, y_r1_b2, 80.0, "SET M10", "CYCLE_LATCH")
        self._line(c, x_fork, y_r1_b2, x5_l, y_r1_b2)
        self._line(c, x5_r, y_r1_b2, set_l, y_r1_b2)
        self._line(c, set_r, y_r1_b2, self.X_RIGHT, y_r1_b2)

        mov_l, mov_r = self._draw_box_instruction(c, 615.0, y_r1_b3, 100.0, "MOV K100 D0", "INIT_SPEED")
        self._line(c, x_fork, y_r1_b3, mov_l, y_r1_b3)
        self._line(c, mov_r, y_r1_b3, self.X_RIGHT, y_r1_b3)

        # RUNG 2: Rising Edge X10 & Word Bit D100.A -> OUT Y17
        y_r2 = 468.0
        c.setFont("Helvetica", 8)
        c.drawString(30.0, self._ry(y_r2 + 3.0), "21")

        x10_l, x10_r = self._draw_contact(c, 180.0, y_r2, "X10", "TRIG_PULSE", mode="RISE")
        d100a_l, d100a_r = self._draw_contact(c, 340.0, y_r2, "D100.A", "STATUS_BIT10", mode="NO")
        y17_l, y17_r = self._draw_coil(c, 680.0, y_r2, "Y17", "DONE_LAMP")

        self._line(c, self.X_LEFT, y_r2, x10_l, y_r2)
        self._line(c, x10_r, y_r2, d100a_l, y_r2)
        self._line(c, d100a_r, y_r2, y17_l, y_r2)
        self._line(c, y17_r, y_r2, self.X_RIGHT, y_r2)

        # Footer Note with unmapped CID glyph token to verify L1 garbled ToUnicode detection
        c.setFont("Helvetica", 7.5)
        c.drawString(70.0, self._ry(560.0), "LEGACY_COMMENT_BLOCK: (cid:1042)(cid:1043)_BROKEN_CMAP_TEST")

        c.showPage()
        c.save()
        return self.output_path
