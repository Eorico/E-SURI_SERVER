"""Turn a filled EQWART workbook into a blank template.

Strips the sample data from every learning-area sheet and repairs any formula
defects found, leaving all formatting, formulas, merged ranges and macros
untouched.

    python scripts/blank_eqwart_template.py "TERM 1 EXAM_ GRADE 11.xlsm" \
        supabase/templates/eqwart_grade11.xlsm

The layout is discovered by `HeuristicAnalyzer`, not hard-coded, so this works
on a reissued workbook too. Administrators normally upload templates through
`POST /admin/report-templates`; this script exists to prepare the one shipped
with the repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.omr.eqwart_export import set_cell  # noqa: E402
from utils.omr.template_analyzer import HeuristicAnalyzer  # noqa: E402
from utils.omr.template_mapping import SheetMapping  # noqa: E402


def blank_sheet(sheet, plan: SheetMapping) -> dict[str, int]:
    counts = {"cleared": 0, "repaired": 0}

    for index in range(plan.max_sections):
        column = plan.first_section_col + index
        letter = plan.section_column_letter(index)

        for coordinate in (
            f"{letter}{plan.section_name_row}",
            f"{letter}{plan.examinee_count_row}",
        ):
            if sheet[coordinate].value is not None:
                set_cell(sheet, coordinate, None)
                counts["cleared"] += 1

        rows = list(range(plan.first_item_row, plan.last_item_row + 1))
        rows += list(range(plan.first_score_row, plan.last_score_row + 1))
        for row in rows:
            cell = sheet.cell(row=row, column=column)
            if cell.value is not None:
                cell.value = None
                counts["cleared"] += 1

    if plan.least_mastered_column and plan.least_mastered_first_row:
        for index in range(plan.least_mastered_count):
            coordinate = f"{plan.least_mastered_column}{plan.least_mastered_first_row + index}"
            if sheet[coordinate].value is not None:
                set_cell(sheet, coordinate, None)
                counts["cleared"] += 1

    for coordinate in list(plan.intervention_activities.values()) + list(
        plan.intervention_modes.values()
    ):
        set_cell(sheet, coordinate, False)
    if plan.intervention_other_cell:
        set_cell(sheet, plan.intervention_other_cell, None)

    for coordinate in plan.signatories.values():
        if sheet[coordinate].value is not None:
            set_cell(sheet, coordinate, None)
            counts["cleared"] += 1

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
                counts["repaired"] += 1

    return counts


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    source, destination = Path(sys.argv[1]), Path(sys.argv[2])
    if not source.exists():
        print(f"Source workbook not found: {source}")
        return 1

    workbook = load_workbook(source, keep_vba=True)
    mapping = HeuristicAnalyzer().analyze(workbook)
    if not mapping.sheets:
        print("No learning-area sheets were recognised in that workbook.")
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    totals = {"cleared": 0, "repaired": 0}

    for name, plan in mapping.sheets.items():
        counts = blank_sheet(workbook[name], plan)
        totals["cleared"] += counts["cleared"]
        totals["repaired"] += counts["repaired"]
        print(
            f"  {name:20s} cleared {counts['cleared']:5d} cells, "
            f"repaired {counts['repaired']:3d} formulas"
        )

    workbook.save(destination)
    print(
        f"\n{len(mapping.sheets)} sheets blanked (confidence {mapping.confidence}), "
        f"{totals['cleared']} values cleared, {totals['repaired']} formulas repaired "
        f"-> {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
