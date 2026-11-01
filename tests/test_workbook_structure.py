"""How a subject becomes a sheet, and a section becomes a column.

The DepEd workbook ships as a single "Blank Sheet": it is a layout, not a
learning area. A subject's page is a copy of it, which is what makes "the
coordinator adds a subject" and "a sheet appears" the same action.

These pin that against the real template file, so a new one from DepEd that
breaks the assumption fails here rather than at export time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "db"
    / "templates"
    / "eqwart_blank_grade11.xlsm"
)


@pytest.fixture(scope="module")
def sheet():
    workbook = load_workbook(TEMPLATE, data_only=False)
    return workbook[workbook.sheetnames[0]]


def test_the_template_is_a_single_layout_sheet():
    workbook = load_workbook(TEMPLATE, data_only=False)
    assert workbook.sheetnames == ["Blank Sheet"]


def test_the_exporter_recognises_it_as_a_layout():
    from utils.omr.eqwart_export import EqwartExporter

    assert "Blank Sheet" in EqwartExporter.LAYOUT_SHEET_NAMES


def test_sections_are_columns_b_to_v(sheet):
    """21 sections, which is what bounds how many classes a subject can hold."""
    assert sheet["A13"].value == "Sections/Classes"
    assert sheet["A14"].value == "No. of Examinees"
    assert "SUM(B14:V14)" in str(sheet["W14"].value)


def test_items_run_down_rows_16_to_75(sheet):
    assert sheet["A15"].value == "Item No."
    assert sheet["A16"].value in (1, "1")
    # The rank formula spans the item block, so it states the last row.
    assert "$X$16:$X$75" in str(sheet["Y16"].value)


def test_the_per_item_columns_are_where_the_exporter_expects(sheet):
    assert sheet["W13"].value == "Total"
    assert sheet["X14"].value == "% of Correct Responses"
    assert sheet["Y14"].value == "Rank"
    assert sheet["Z14"].value == "Level of Mastery"
    assert sheet["AA14"].value == "Instructional Assistance"


def test_the_statistics_block_is_intact(sheet):
    for row, label in [
        (76, "SUM OF ITEM ANALYSIS"),
        (77, "MEAN"),
        (78, "SD"),
        (79, "MPS"),
    ]:
        assert sheet[f"A{row}"].value == label


def test_five_least_mastered_slots_for_the_coordinator(sheet):
    """The coordinator encodes these from the result; there are exactly five."""
    assert sheet["A83"].value == "LEAST MASTERED COMPETENCY"
    for offset in range(5):
        label = str(sheet[f"A{84 + offset}"].value or "")
        assert f"#{offset + 1}" in label


def test_scores_are_encoded_per_examinee_in_step_two(sheet):
    assert "STEP 2" in str(sheet["A117"].value)
    assert sheet["A120"].value == "Sections/Classes"
    assert sheet["A122"].value == "Examinee No."
    # 70 examinee rows, 123..192.
    assert "B123:B192" in str(sheet["B199"].value)


def test_the_proficiency_bands_come_from_the_legend_not_from_code(sheet):
    """Band edges live in B210:B214, so DepEd can move them without a release."""
    assert sheet["B208"].value == "KS4" or sheet["B208"].value == "Legend"
    for row in range(210, 214):
        assert isinstance(sheet[f"B{row}"].value, (int, float))


def test_the_section_count_formula_is_narrower_than_the_section_range(sheet):
    """A defect in the supplied template, recorded rather than silently fixed.

    B9 counts B14:F14 - five sections - while every other formula spans
    B..V (21). A school with six or more sections would report the wrong
    total. The exporter writes the count itself, so the export is right; this
    test exists so the discrepancy is not mistaken for our bug.
    """
    assert "B14:F14" in str(sheet["B9"].value)
    assert "B14:V14" in str(sheet["W14"].value)


# --------------------------------------------------------------------------- #
# A subject becomes a sheet                                                    #
# --------------------------------------------------------------------------- #
def export_for(*subjects: tuple[str, str, list[int]]):
    """Export the named subjects from the blank template."""
    import io

    from openpyxl import load_workbook as load

    from utils.omr.eqwart_export import EqwartExporter, EqwartSheetData, SectionColumn
    from utils.omr.template_analyzer import HeuristicAnalyzer

    mapping = HeuristicAnalyzer().analyze(load(TEMPLATE, data_only=False))
    sheets = [
        EqwartSheetData(
            sheet_name=name,
            division="Dasmarinas City",
            cluster="CLUSTER VIII",
            school="PALIPARAN III SHS",
            school_year="2026-2027",
            learning_area=name,
            grade_level=11,
            item_count=60,
            sections=[
                SectionColumn(
                    name=section,
                    examinee_count=len(scores),
                    item_frequencies=[2] * 60,
                    raw_scores=scores,
                )
            ],
        )
        for name, section, scores in subjects
    ]
    return load(io.BytesIO(EqwartExporter(TEMPLATE, mapping).export(sheets)), data_only=False)


def test_each_subject_gets_its_own_sheet():
    """The coordinator adding a subject is what makes its sheet exist."""
    workbook = export_for(
        ("General Science", "ACAD STEM 11-1", [55, 42, 30]),
        ("General Mathematics", "ACAD ABM 11-1", [48, 22]),
    )
    assert workbook.sheetnames == ["General Science", "General Mathematics"]


def test_the_blank_layout_does_not_travel_to_csdo():
    """It is scaffolding, not a learning area."""
    workbook = export_for(("General Science", "ACAD STEM 11-1", [55]))
    assert "Blank Sheet" not in workbook.sheetnames


def test_a_section_becomes_a_column_on_its_subject_sheet():
    workbook = export_for(
        ("General Science", "ACAD STEM 11-1", [55, 42, 30]),
        ("General Mathematics", "ACAD ABM 11-1", [48, 22]),
    )
    science = workbook["General Science"]
    assert science["B5"].value == "General Science"
    assert science["B13"].value == "ACAD STEM 11-1"
    assert science["B14"].value == 3

    # Each subject keeps its own sections; the copy is not shared state.
    assert workbook["General Mathematics"]["B13"].value == "ACAD ABM 11-1"


def test_scores_land_in_the_step_two_block():
    workbook = export_for(("General Science", "ACAD STEM 11-1", [55, 42, 30]))
    science = workbook["General Science"]
    assert [science[f"B{123 + i}"].value for i in range(3)] == [55, 42, 30]


def test_a_cloned_sheet_reuses_the_layout_geometry():
    """The mapping only ever describes the blank sheet, because that is the
    only sheet the template shipped with."""
    from openpyxl import load_workbook as load

    from utils.omr.template_analyzer import HeuristicAnalyzer

    mapping = HeuristicAnalyzer().analyze(load(TEMPLATE, data_only=False))
    assert list(mapping.sheets) == ["Blank Sheet"]
    assert mapping.confidence == pytest.approx(1.0)
