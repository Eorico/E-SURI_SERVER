"""EQWART workbook export.

Opens a report template and writes computed values into the correct cells of
each learning area's sheet, leaving every formula, format and merged range
intact so MPS, SD and rank recalculate when Excel opens the file.

Where those cells are is **not** hard-coded: the exporter is driven by a
`TemplateMapping`, produced when an administrator uploads the template. A new
DepEd layout therefore needs a new mapping, not a code change (OCP).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from utils.omr.template_mapping import MappingError, SheetMapping, TemplateMapping


def set_cell(sheet: Worksheet, coordinate: str, value) -> None:
    """Write a value, resolving a merged range to its anchor cell.

    Several template cells (the "Other" intervention box, some signatory
    lines) are merged, and openpyxl refuses writes to anything but the
    top-left member.
    """
    for merged in sheet.merged_cells.ranges:
        if coordinate in merged:
            sheet.cell(row=merged.min_row, column=merged.min_col, value=value)
            return
    sheet[coordinate] = value


@dataclass
class SectionColumn:
    """One section: a column in both tables of the sheet."""

    name: str
    examinee_count: int
    #: Correct-response frequency per item, in item order.
    item_frequencies: list[int] = field(default_factory=list)
    #: Raw scores, one per examinee.
    raw_scores: list[int] = field(default_factory=list)


@dataclass
class EqwartSheetData:
    """Everything one learning-area sheet needs."""

    sheet_name: str
    division: str
    cluster: str
    school: str
    school_year: str
    learning_area: str
    grade_level: int
    item_count: int
    sections: list[SectionColumn]
    least_mastered: list[str] = field(default_factory=list)
    intervention_activities: list[str] = field(default_factory=list)
    intervention_other: str | None = None
    intervention_mode: str | None = None
    key_teacher: str | None = None
    reviewed_by: str | None = None
    approved_by: str | None = None

    def header_values(self) -> dict[str, object]:
        """Header field name to value, matching `SheetMapping.header` keys."""
        return {
            "division": self.division,
            "cluster": self.cluster,
            "school": self.school,
            "school_year": self.school_year,
            "learning_area": self.learning_area,
            "grade_level": self.grade_level,
            "item_count": self.item_count,
        }


class TemplateCapacityError(ValueError):
    """More sections or examinees than the template can hold."""


class EqwartExporter:
    """Writes computed values into a copy of a mapped template."""

    def __init__(self, template: str | io.BytesIO, mapping: TemplateMapping):
        """`template` is a path on disk or an in-memory workbook."""
        self.template = template
        self.mapping = mapping

    #: Sheets that are a layout to copy rather than a learning area's own page.
    #: DepEd ships the workbook as one of these; a subject's sheet is a copy of
    #: it, which is why adding a subject adds a sheet.
    LAYOUT_SHEET_NAMES = ("Blank Sheet", "BLANK SHEET", "Blank", "Template")

    def export(self, sheets: list[EqwartSheetData]) -> bytes:
        workbook = load_workbook(self.template, keep_vba=True)
        layout = self._layout_sheet(workbook)

        for data in sheets:
            if data.sheet_name not in workbook.sheetnames:
                if layout is None:
                    raise KeyError(
                        f"The template has no sheet named {data.sheet_name!r}, and no "
                        f"blank sheet to copy one from."
                    )
                # A new subject gets its own page, cut from the blank one, so
                # the formulas and the legend come with it.
                copy = workbook.copy_worksheet(workbook[layout])
                copy.title = data.sheet_name
            self._write_sheet(
                workbook[data.sheet_name], data, self._plan_for(data.sheet_name, layout)
            )

        # The layout itself is scaffolding, not a learning area, so it does not
        # travel to CSDO - unless removing it would leave an empty workbook.
        if layout is not None and layout not in {data.sheet_name for data in sheets}:
            if len(workbook.sheetnames) > 1:
                del workbook[layout]

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def _plan_for(self, sheet_name: str, layout: str | None) -> SheetMapping:
        """The geometry to write this sheet with.

        A sheet copied from the layout has the layout's geometry, so it uses
        the layout's plan — the mapping only ever describes the blank sheet,
        because that is the only one the template shipped with.
        """
        try:
            return self.mapping.sheet_for(sheet_name)
        except MappingError:
            if layout is None:
                raise
            return self.mapping.sheet_for(layout)

    def _layout_sheet(self, workbook) -> str | None:
        for name in self.LAYOUT_SHEET_NAMES:
            if name in workbook.sheetnames:
                return name
        # A workbook with exactly one sheet is itself the layout.
        return workbook.sheetnames[0] if len(workbook.sheetnames) == 1 else None

    # --- per sheet --------------------------------------------------------
    def _write_sheet(self, sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        self._repair_formulas(sheet, plan)
        self._write_header(sheet, data, plan)
        self._write_sections(sheet, data, plan)
        self._write_least_mastered(sheet, data, plan)
        self._write_intervention(sheet, data, plan)
        self._write_signatories(sheet, data, plan)

    @staticmethod
    def _repair_formulas(sheet: Worksheet, plan: SheetMapping) -> None:
        """Apply any known formula corrections recorded for this template."""
        if not plan.formula_repairs or not plan.repair_rows:
            return
        for row in plan.repair_rows:
            for column in range(plan.first_section_col, plan.last_section_col + 1):
                cell = sheet.cell(row=row, column=column)
                if not isinstance(cell.value, str):
                    continue
                repaired = cell.value
                for wrong, right in plan.formula_repairs:
                    repaired = repaired.replace(wrong, right)
                if repaired != cell.value:
                    cell.value = repaired

    @staticmethod
    def _write_header(sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        values = data.header_values()
        for field_name, coordinate in plan.header.items():
            if field_name in values:
                set_cell(sheet, coordinate, values[field_name])

    def _write_sections(self, sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        if len(data.sections) > plan.max_sections:
            raise TemplateCapacityError(
                f"{plan.sheet_name}: the template holds {plan.max_sections} sections; "
                f"{len(data.sections)} were supplied."
            )
        for section in data.sections:
            if len(section.raw_scores) > plan.max_examinees:
                raise TemplateCapacityError(
                    f"{plan.sheet_name}/{section.name}: the template holds "
                    f"{plan.max_examinees} examinees; {len(section.raw_scores)} were supplied."
                )

        self._clear_data_area(sheet, plan)

        for index, section in enumerate(data.sections):
            column = plan.first_section_col + index
            letter = plan.section_column_letter(index)

            set_cell(sheet, f"{letter}{plan.section_name_row}", section.name)
            set_cell(sheet, f"{letter}{plan.examinee_count_row}", section.examinee_count)

            for offset, frequency in enumerate(section.item_frequencies):
                row = plan.first_item_row + offset
                if row > plan.last_item_row:
                    break
                sheet.cell(row=row, column=column, value=frequency)

            for offset, score in enumerate(sorted(section.raw_scores, reverse=True)):
                row = plan.first_score_row + offset
                if row > plan.last_score_row:
                    break
                sheet.cell(row=row, column=column, value=score)

    @staticmethod
    def _clear_data_area(sheet: Worksheet, plan: SheetMapping) -> None:
        """Blank any sample data so a short run cannot inherit old numbers."""
        for index in range(plan.max_sections):
            column = plan.first_section_col + index
            letter = plan.section_column_letter(index)
            set_cell(sheet, f"{letter}{plan.section_name_row}", None)
            set_cell(sheet, f"{letter}{plan.examinee_count_row}", None)
            for row in range(plan.first_item_row, plan.last_item_row + 1):
                sheet.cell(row=row, column=column, value=None)
            for row in range(plan.first_score_row, plan.last_score_row + 1):
                sheet.cell(row=row, column=column, value=None)

    @staticmethod
    def _write_least_mastered(sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        if plan.least_mastered_column is None or plan.least_mastered_first_row is None:
            return
        for index in range(plan.least_mastered_count):
            coordinate = f"{plan.least_mastered_column}{plan.least_mastered_first_row + index}"
            value = data.least_mastered[index] if index < len(data.least_mastered) else None
            set_cell(sheet, coordinate, value)

    @staticmethod
    def _write_intervention(sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        chosen = set(data.intervention_activities)
        for label, coordinate in plan.intervention_activities.items():
            set_cell(sheet, coordinate, label in chosen)

        if plan.intervention_other_cell and data.intervention_other is not None:
            set_cell(sheet, plan.intervention_other_cell, data.intervention_other)

        for label, coordinate in plan.intervention_modes.items():
            set_cell(sheet, coordinate, data.intervention_mode == label)

    @staticmethod
    def _write_signatories(sheet: Worksheet, data: EqwartSheetData, plan: SheetMapping) -> None:
        values = {
            "key_teacher": data.key_teacher,
            "reviewed_by": data.reviewed_by,
            "approved_by": data.approved_by,
        }
        for field_name, coordinate in plan.signatories.items():
            if values.get(field_name):
                set_cell(sheet, coordinate, values[field_name])
