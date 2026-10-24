"""ETWART workbook export (Phase 7).

Opens the blanked ETWART template and writes computed values into the correct
cells per subject sheet (examinee counts, item frequencies, raw scores), leaving
the template formulas intact so MPS/SD/ranks recalculate on open.
"""
from __future__ import annotations


class EtwartExporter:
    def __init__(self, template_path: str):
        self.template_path = template_path

    def export(self, assessment_id) -> bytes:
        raise NotImplementedError("Phase 7: write analysis into the ETWART template")
