"""Printable answer-sheet generator.

Renders a ReportLab PDF from a `SheetLayout`: corner fiducials, the write-in
name / section / quiz boxes, a QR code carrying `test_code|student_no`, the
bubbled student-ID grid, and the A–E bubble columns.

Geometry comes from the shared layout, so a bubble is printed exactly where the
scanner will look for it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import black, white
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.omr.sheet_layout import SheetLayout


@dataclass(frozen=True)
class SheetSubject:
    """Who and what one printed sheet is for."""

    student_no: str
    student_name: str
    section_name: str
    assessment_title: str
    test_code: str


class AnswerSheetRenderer:
    """Draws one or many sheets into a single PDF."""

    def __init__(self, layout: SheetLayout):
        self.layout = layout

    def render(self, subject: SheetSubject) -> bytes:
        return self.render_many([subject])

    def render_many(self, subjects: list[SheetSubject]) -> bytes:
        buffer = io.BytesIO()
        page = (self.layout.page_width_mm * mm, self.layout.page_height_mm * mm)
        pdf = canvas.Canvas(buffer, pagesize=page)

        for subject in subjects:
            self._draw_sheet(pdf, subject)
            pdf.showPage()

        pdf.save()
        return buffer.getvalue()

    # --- drawing ----------------------------------------------------------
    def _draw_sheet(self, pdf: canvas.Canvas, subject: SheetSubject) -> None:
        self._draw_fiducials(pdf)
        self._draw_header(pdf, subject)
        self._draw_code_strip(pdf, subject)
        self._draw_bubbles(pdf)
        self._draw_footer(pdf)

    def _draw_fiducials(self, pdf: canvas.Canvas) -> None:
        """Solid squares at the corners and mid-edges, used to deskew the scan."""
        size = self.layout.fiducial_mm * mm
        pdf.setFillColor(black)
        for x, y in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0),
                     (0.5, 0.0), (0.5, 1.0)):
            mm_x, mm_y = self.layout.to_mm(x, y)
            pdf.rect(mm_x * mm - size / 2, mm_y * mm - size / 2, size, size, stroke=0, fill=1)

    def _draw_header(self, pdf: canvas.Canvas, subject: SheetSubject) -> None:
        left, top = self.layout.to_mm(0.06, 0.035)
        box_height = 9 * mm
        pdf.setFillColor(black)

        def labelled_box(x_mm: float, y_mm: float, width_mm: float, label: str, value: str) -> None:
            pdf.setFont("Helvetica", 7)
            pdf.drawString(x_mm * mm, y_mm * mm + box_height + 1.5 * mm, label)
            pdf.setLineWidth(0.8)
            pdf.rect(x_mm * mm, y_mm * mm, width_mm * mm, box_height, stroke=1, fill=0)
            pdf.setFont("Helvetica-Bold", 10)
            pdf.drawString(x_mm * mm + 2 * mm, y_mm * mm + 2.8 * mm, value)

        labelled_box(left, top - 9, 78, "Name", subject.student_name)
        labelled_box(left + 84, top - 9, 54, "Section", subject.section_name)
        labelled_box(left, top - 22, 78, "Student No.", subject.student_no)
        labelled_box(left + 84, top - 22, 54, "Score", "")

    def _draw_code_strip(self, pdf: canvas.Canvas, subject: SheetSubject) -> None:
        """QR plus the human-readable code, so a sheet is identifiable either way."""
        payload = f"{subject.test_code}|{subject.student_no}"
        widget = qr.QrCodeWidget(payload, barLevel="M")
        bounds = widget.getBounds()
        target = 22 * mm
        drawing = Drawing(
            target,
            target,
            transform=[
                target / (bounds[2] - bounds[0]), 0, 0,
                target / (bounds[3] - bounds[1]), -bounds[0], -bounds[1],
            ],
        )
        drawing.add(widget)

        x_mm, y_mm = self.layout.to_mm(0.88, 0.035)
        renderPDF.draw(drawing, pdf, x_mm * mm - target / 2, y_mm * mm - target)

        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawCentredString(x_mm * mm, y_mm * mm - target - 4 * mm, subject.test_code)

        # Assessment name down the right margin, as on the reference sheets.
        pdf.saveState()
        right_mm, mid_mm = self.layout.to_mm(1.0, 0.55)
        pdf.translate(right_mm * mm + 4 * mm, mid_mm * mm)
        pdf.rotate(90)
        pdf.setFont("Helvetica", 9)
        pdf.drawCentredString(0, 0, f"{subject.assessment_title} ({subject.test_code})")
        pdf.restoreState()

    def _draw_bubbles(self, pdf: canvas.Canvas) -> None:
        radius = (self.layout.bubble_diameter_mm / 2) * mm
        grouped = self.layout.grouped_bubbles()

        pdf.setLineWidth(0.6)
        for item_no, bubbles in sorted(grouped.items()):
            first = bubbles[0]
            number_x, number_y = self.layout.to_mm(first.x, first.y)
            pdf.setFillColor(black)
            pdf.setFont("Helvetica", 6.5)
            pdf.drawRightString(number_x * mm - radius - 1.6 * mm, number_y * mm - 1.6, str(item_no))

            for bubble in bubbles:
                mm_x, mm_y = self.layout.to_mm(bubble.x, bubble.y)
                pdf.setFillColor(white)
                pdf.setStrokeColor(black)
                pdf.circle(mm_x * mm, mm_y * mm, radius, stroke=1, fill=1)
                pdf.setFillColor(black)
                pdf.setFont("Helvetica", 4.4)
                pdf.drawCentredString(mm_x * mm, mm_y * mm - 1.5, bubble.option)

    def _draw_footer(self, pdf: canvas.Canvas) -> None:
        x_mm, y_mm = self.layout.to_mm(0.06, 0.985)
        pdf.setFillColor(black)
        pdf.setFont("Helvetica", 6)
        pdf.drawString(
            x_mm * mm,
            y_mm * mm,
            "Shade one circle per item with a dark pencil. Do not fold or crease this sheet.",
        )
