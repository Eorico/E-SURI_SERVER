"""Read the structure of an uploaded report template.

Two jobs:

* `build_digest` produces a compact, **data-free** description of a workbook —
  labels, formulas, merged ranges and a density map. This is the only thing
  ever sent to an AI model, so no learner name or score leaves the server.
* `HeuristicAnalyzer` turns a workbook into a `TemplateMapping` by locating
  anchors from their printed labels and formulas. It handles a reworded,
  reordered or resized template on its own, and is the fallback whenever the
  AI mapper is unavailable or produces something that fails validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from utils.omr.template_mapping import SheetMapping, TemplateMapping

#: Cap the digest so a hostile or enormous workbook cannot blow up a prompt.
MAX_LABELS_PER_SHEET = 400
MAX_FORMULAS_PER_SHEET = 120
MAX_SCAN_ROWS = 400
MAX_SCAN_COLS = 60

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise(text: Any) -> str:
    """Casefolded, punctuation-free text for tolerant label matching."""
    if text is None:
        return ""
    collapsed = _PUNCT.sub(" ", str(text).casefold())
    return _SPACES.sub(" ", collapsed).strip()


def matches(text: Any, *needles: str) -> bool:
    haystack = normalise(text)
    return any(normalise(needle) in haystack for needle in needles if needle)


@dataclass(frozen=True)
class LabelHit:
    row: int
    column: int
    text: str

    @property
    def coordinate(self) -> str:
        return f"{get_column_letter(self.column)}{self.row}"


# --------------------------------------------------------------------------- #
# Digest                                                                       #
# --------------------------------------------------------------------------- #
def build_digest(workbook: Workbook) -> dict[str, Any]:
    """A data-free description of the workbook, safe to send to a model."""
    return {
        "sheets": [
            _sheet_digest(workbook[name])
            for name in workbook.sheetnames
            if workbook[name].sheet_state == "visible" or _looks_like_data_sheet(workbook[name])
        ]
    }


def _sheet_digest(sheet: Worksheet) -> dict[str, Any]:
    labels: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []
    density: list[dict[str, int]] = []

    max_row = min(sheet.max_row or 1, MAX_SCAN_ROWS)
    max_col = min(sheet.max_column or 1, MAX_SCAN_COLS)

    for row in range(1, max_row + 1):
        numeric = 0
        first_numeric = last_numeric = 0
        for column in range(1, max_col + 1):
            value = sheet.cell(row=row, column=column).value
            if value is None:
                continue

            if isinstance(value, str) and value.startswith("="):
                if len(formulas) < MAX_FORMULAS_PER_SHEET:
                    formulas.append(
                        {
                            "cell": f"{get_column_letter(column)}{row}",
                            # Truncated: the shape matters, not the whole expression.
                            "formula": value[:120],
                        }
                    )
            elif isinstance(value, str):
                if len(labels) < MAX_LABELS_PER_SHEET and value.strip():
                    labels.append(
                        {"cell": f"{get_column_letter(column)}{row}", "text": value.strip()[:120]}
                    )
            elif isinstance(value, (int, float)):
                numeric += 1
                first_numeric = first_numeric or column
                last_numeric = column

        if numeric:
            density.append(
                {"row": row, "numeric": numeric, "from_col": first_numeric, "to_col": last_numeric}
            )

    return {
        "name": sheet.title,
        "state": sheet.sheet_state,
        "max_row": sheet.max_row,
        "max_column": sheet.max_column,
        "labels": labels,
        "formulas": formulas,
        "numeric_density": density[:MAX_SCAN_ROWS],
        "merged": [str(r) for r in list(sheet.merged_cells.ranges)[:60]],
    }


def _looks_like_data_sheet(sheet: Worksheet) -> bool:
    """A learning-area sheet carries both tables; a how-to tab does not."""
    labels = _collect_labels(sheet)
    has_item_table = _find_label(labels, "item analysis", "frequency of correct") is not None
    has_scores = _find_label(labels, "scores obtained", "examinee no", "assessment scores") is not None
    return has_item_table and has_scores


# --------------------------------------------------------------------------- #
# Heuristic analyser                                                           #
# --------------------------------------------------------------------------- #
class HeuristicAnalyzer:
    """Finds the anchors by their printed labels, not by fixed addresses."""

    HEADER_LABELS: dict[str, tuple[str, ...]] = {
        "division": ("division",),
        "cluster": ("cluster",),
        "school": ("school",),
        "school_year": ("school year",),
        "learning_area": ("learning area", "subject"),
        "grade_level": ("grade level",),
        "item_count": ("total no of items", "number of items", "total items"),
        "total_examinees": ("total no of examinees", "total examinees"),
        "total_sections": ("total no of sections", "total sections"),
    }

    ACTIVITY_LABELS = (
        "Reteaching",
        "Using alternative instructional strategies",
        "Task analysis",
        "Additional practice",
        "One-on-one tutoring",
        "Other",
    )
    MODE_LABELS = ("Add-on", "Pull-out", "Built-in")

    SIGNATORY_LABELS: dict[str, tuple[str, ...]] = {
        "key_teacher": ("learning area key teacher", "key teacher"),
        "reviewed_by": ("master teacher", "head teacher"),
        "approved_by": ("school head", "principal"),
    }

    def analyze(self, workbook: Workbook) -> TemplateMapping:
        sheets: dict[str, SheetMapping] = {}
        notes: list[str] = []

        for name in workbook.sheetnames:
            sheet = workbook[name]
            if not _looks_like_data_sheet(sheet):
                continue
            try:
                sheets[name] = self.analyze_sheet(sheet)
            except Exception as exc:  # one bad sheet must not lose the rest
                notes.append(f"{name}: could not be mapped ({exc}).")

        confidence = self._confidence(sheets, notes)
        if not sheets:
            notes.append("No sheet carried both an item-analysis table and a score table.")

        return TemplateMapping(
            sheets=sheets, source="heuristic", confidence=confidence, notes=notes
        )

    # --- one sheet --------------------------------------------------------
    def analyze_sheet(self, sheet: Worksheet) -> SheetMapping:
        labels = _collect_labels(sheet)

        table_top = self._table_top(labels)
        section_row = self._row_of(
            labels, "sections classes", "section class", "sections", min_row=table_top
        )
        examinee_row = self._row_of(
            labels, "no of examinees", "number of examinees", min_row=section_row or table_top
        )
        item_label = _find_label(labels, "item no", "item number")
        examinee_label = _find_label(labels, "examinee no", "examinee number")

        if section_row is None or item_label is None:
            raise ValueError("no section row or item-number label")

        examinee_row = examinee_row or section_row + 1
        first_item_row = item_label.row + 1
        last_item_row = self._block_end(sheet, first_item_row, labels)

        if examinee_label is None:
            raise ValueError("no examinee-number label for the score table")
        first_score_row = examinee_label.row + 1
        last_score_row = self._block_end(sheet, first_score_row, labels)

        first_col, last_col = self._section_columns(sheet, section_row, examinee_row)

        lmc_column, lmc_first_row, lmc_count = self._least_mastered(sheet, labels)
        repairs, repair_rows = detect_formula_repairs(sheet)

        return SheetMapping(
            sheet_name=sheet.title,
            section_name_row=section_row,
            examinee_count_row=examinee_row,
            first_section_col=first_col,
            last_section_col=last_col,
            first_item_row=first_item_row,
            last_item_row=last_item_row,
            first_score_row=first_score_row,
            last_score_row=last_score_row,
            header=self._header_cells(sheet, labels),
            least_mastered_column=lmc_column,
            least_mastered_first_row=lmc_first_row,
            least_mastered_count=lmc_count,
            intervention_activities=self._checkbox_cells(sheet, labels, self.ACTIVITY_LABELS),
            intervention_modes=self._checkbox_cells(sheet, labels, self.MODE_LABELS),
            intervention_other_cell=self._other_cell(labels),
            signatories=self._signatory_cells(sheet, labels),
            learning_area_hint=self._header_value(sheet, labels, "learning_area"),
            formula_repairs=repairs,
            repair_rows=repair_rows,
        )

    @staticmethod
    def _table_top(labels: list[LabelHit]) -> int:
        """Row of the item-analysis caption; everything above it is the header.

        The caption sits in the left-hand columns. An instruction panel off to
        the right may also say "item analysis", so the search is column-bound.
        """
        hit = _find_label(labels, "frequency of correct", max_col=8) or _find_label(
            labels, "item analysis", max_col=8
        )
        return hit.row if hit else 1

    # --- pieces -----------------------------------------------------------
    def _header_cells(self, sheet: Worksheet, labels: list[LabelHit]) -> dict[str, str]:
        """A header label sits to the left of its value."""
        found: dict[str, str] = {}
        for field, needles in self.HEADER_LABELS.items():
            hit = _find_label(labels, *needles, max_row=20)
            if hit is None:
                continue
            for column in range(hit.column + 1, min(hit.column + 6, MAX_SCAN_COLS)):
                cell = sheet.cell(row=hit.row, column=column)
                if cell.value is not None or _is_merged_anchor(sheet, cell.row, cell.column):
                    found[field] = f"{get_column_letter(column)}{hit.row}"
                    break
            else:
                found[field] = f"{get_column_letter(hit.column + 1)}{hit.row}"
        return found

    def _header_value(self, sheet: Worksheet, labels: list[LabelHit], field: str) -> str | None:
        cells = self._header_cells(sheet, labels)
        if (coordinate := cells.get(field)) is None:
            return None
        value = sheet[coordinate].value
        return str(value) if value is not None and not str(value).startswith("=") else None

    @staticmethod
    def _section_columns(sheet: Worksheet, section_row: int, examinee_row: int) -> tuple[int, int]:
        """Columns between the label and the "Total" column.

        A blank template has no section names, so the examinee-count row's
        formulas and the width of the item table are used as well.
        """
        first = 2
        last = None
        for column in range(2, MAX_SCAN_COLS):
            value = sheet.cell(row=section_row, column=column).value
            if isinstance(value, str) and matches(value, "total", "overall"):
                last = column - 1
                break

        if last is None:
            # Fall back to the width quoted inside the row's own formulas,
            # e.g. =SUM(B14:V14) gives V as the final section column.
            formula = sheet.cell(row=examinee_row, column=23).value or ""
            match = re.search(r"[A-Z]+\d+:([A-Z]+)\d+", str(formula))
            if match:
                from openpyxl.utils import column_index_from_string

                last = column_index_from_string(match.group(1))
        return first, last or 22

    @staticmethod
    def _block_end(sheet: Worksheet, first_row: int, labels: list[LabelHit]) -> int:
        """A data block runs until the next labelled row in column A."""
        for hit in sorted(labels, key=lambda h: h.row):
            if hit.column == 1 and hit.row > first_row and not _is_numeric_label(hit.text):
                return hit.row - 1
        return min(sheet.max_row or first_row, first_row + MAX_SCAN_ROWS)

    @staticmethod
    def _least_mastered(
        sheet: Worksheet, labels: list[LabelHit]
    ) -> tuple[str | None, int | None, int]:
        hits = [
            hit
            for hit in labels
            if hit.column == 1
            and matches(hit.text, "least mastered competency")
            # The caption has no number; the five entries are "#1".."#5".
            and any(ch.isdigit() for ch in hit.text)
        ]
        rows = sorted({hit.row for hit in hits})
        if not rows:
            return None, None, 5

        header = _find_label(labels, "competency and course code", "competency and course")
        column = get_column_letter(header.column) if header else "E"
        return column, rows[0], max(1, len(rows))

    @staticmethod
    def _checkbox_cells(
        sheet: Worksheet, labels: list[LabelHit], wanted: Iterable[str]
    ) -> dict[str, str]:
        """A checkbox is the first boolean to the right of its label."""
        found: dict[str, str] = {}
        for label in wanted:
            hit = _find_label(labels, label)
            if hit is None:
                continue
            for column in range(hit.column + 1, min(hit.column + 12, MAX_SCAN_COLS)):
                if isinstance(sheet.cell(row=hit.row, column=column).value, bool):
                    found[label] = f"{get_column_letter(column)}{hit.row}"
                    break
        return found

    @staticmethod
    def _other_cell(labels: list[LabelHit]) -> str | None:
        hit = _find_label(labels, "other pls indicate", "other please indicate", "others")
        return hit.coordinate if hit else None

    def _signatory_cells(self, sheet: Worksheet, labels: list[LabelHit]) -> dict[str, str]:
        """The printed name sits on the row above its role caption."""
        found: dict[str, str] = {}
        block_top = max(
            (hit.row for hit in labels if matches(hit.text, "intervention", "least mastered")),
            default=1,
        )
        for field, needles in self.SIGNATORY_LABELS.items():
            hit = _find_label(labels, *needles, min_row=block_top, max_col=8)
            if hit is None or hit.row < 2:
                continue
            found[field] = f"{get_column_letter(hit.column)}{hit.row - 1}"
        return found

    @staticmethod
    def _row_of(labels: list[LabelHit], *needles: str, min_row: int | None = None) -> int | None:
        hit = _find_label(labels, *needles, min_row=min_row)
        return hit.row if hit else None

    @staticmethod
    def _confidence(sheets: dict[str, SheetMapping], notes: list[str]) -> float:
        """How much of each mapping was actually found, averaged."""
        if not sheets:
            return 0.0

        scores = []
        for mapping in sheets.values():
            present = [
                bool(mapping.header),
                mapping.least_mastered_column is not None,
                bool(mapping.intervention_activities),
                bool(mapping.intervention_modes),
                bool(mapping.signatories),
                not mapping.validate(),
            ]
            scores.append(sum(present) / len(present))
        penalty = 0.05 * len(notes)
        return round(max(0.0, min(1.0, sum(scores) / len(scores) - penalty)), 3)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _collect_labels(sheet: Worksheet) -> list[LabelHit]:
    hits: list[LabelHit] = []
    max_row = min(sheet.max_row or 1, MAX_SCAN_ROWS)
    max_col = min(sheet.max_column or 1, MAX_SCAN_COLS)

    for row in range(1, max_row + 1):
        for column in range(1, max_col + 1):
            value = sheet.cell(row=row, column=column).value
            if isinstance(value, str) and value.strip() and not value.startswith("="):
                hits.append(LabelHit(row=row, column=column, text=value.strip()))
    return hits


def _find_label(
    labels: list[LabelHit],
    *needles: str,
    max_row: int | None = None,
    min_row: int | None = None,
    max_col: int | None = None,
) -> LabelHit | None:
    for hit in labels:
        if max_row is not None and hit.row > max_row:
            continue
        if min_row is not None and hit.row < min_row:
            continue
        if max_col is not None and hit.column > max_col:
            continue
        if matches(hit.text, *needles):
            return hit
    return None


def _is_numeric_label(text: str) -> bool:
    """Row numbers like "1" or formulas-as-text are not block boundaries."""
    stripped = text.strip()
    return stripped.isdigit() or stripped.startswith("=")


def _is_merged_anchor(sheet: Worksheet, row: int, column: int) -> bool:
    return any(
        merged.min_row == row and merged.min_col == column for merged in sheet.merged_cells.ranges
    )

# --------------------------------------------------------------------------- #
# Formula defect detection                                                     #
# --------------------------------------------------------------------------- #
#: The proficiency bands, used to find the band-count rows by their labels.
_BAND_LABELS = (
    "highly proficient",
    "not proficient",
    "low proficient",
    "nearly proficient",
    "proficient",
)

_CELL_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d{1,7})")


def detect_formula_repairs(sheet: Worksheet) -> tuple[list[tuple[str, str]], list[int]]:
    """Find broken comparisons in the band-count rows.

    Two defects turn up in real DepEd workbooks and both silently drop learners
    from a band:

    * a cut-off reference with a stray digit (``$B$2111`` for ``$B$211``), which
      points at an empty cell so the condition matches nothing;
    * ``">"`` where its sibling rows use ``">="``, which excludes anyone sitting
      exactly on the cut.

    Returns the (wrong, right) replacements and the rows to apply them to, ready
    to store on the mapping.
    """
    band_rows = [
        hit.row
        for hit in _collect_labels(sheet)
        if hit.column == 1 and matches(hit.text, *_BAND_LABELS)
    ]
    if not band_rows:
        return [], []

    repairs: set[tuple[str, str]] = set()
    affected: set[int] = set()
    max_row = sheet.max_row or 1
    operators: dict[int, set[str]] = {}

    for row in band_rows:
        for column in range(2, min(sheet.max_column or 2, MAX_SCAN_COLS) + 1):
            formula = sheet.cell(row=row, column=column).value
            if not isinstance(formula, str) or not formula.startswith("="):
                continue

            # Dangling cut-off reference: the row number has an extra digit.
            for letters, digits in _CELL_REF.findall(formula):
                target_row = int(digits)
                if target_row <= max_row:
                    continue
                trimmed = digits[:-1]
                if trimmed and int(trimmed) <= max_row:
                    repairs.add((f"${letters}${digits}", f"${letters}${trimmed}"))
                    affected.add(row)

            if '">="&' in formula:
                operators.setdefault(row, set()).add(">=")
            elif '">"&' in formula:
                operators.setdefault(row, set()).add(">")

    # A lone ">" among ">=" siblings is the off-by-one, not a deliberate choice.
    strict_rows = {row for row, ops in operators.items() if ops == {">"}}
    inclusive_rows = {row for row, ops in operators.items() if ">=" in ops}
    if strict_rows and inclusive_rows:
        for row in strict_rows:
            for column in range(2, min(sheet.max_column or 2, MAX_SCAN_COLS) + 1):
                formula = sheet.cell(row=row, column=column).value
                if isinstance(formula, str) and '">"&' in formula:
                    for letters, digits in _CELL_REF.findall(formula.split('">"&')[1][:12]):
                        repairs.add((f'">"&${letters}${digits}', f'">="&${letters}${digits}'))
                        affected.add(row)
                    break

    return sorted(repairs), sorted(affected)
