"""Where everything lives inside a report template.

A `TemplateMapping` is what used to be hard-coded constants in the exporter.
It is produced once when an administrator uploads a workbook — by the AI mapper
or the heuristic analyser — then stored, versioned and reused for every export.

DepEd reissues the workbook most years. Because the exporter reads this object
rather than fixed cell addresses, a new layout needs a new mapping and no code
change (OCP).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from openpyxl.utils import column_index_from_string, get_column_letter

#: The header fields a sheet is expected to carry. Anything missing simply is
#: not written, so an older or trimmed template still exports.
HEADER_FIELDS = (
    "division",
    "cluster",
    "school",
    "school_year",
    "learning_area",
    "grade_level",
    "item_count",
    "total_examinees",
    "total_sections",
)

SIGNATORY_FIELDS = ("key_teacher", "reviewed_by", "approved_by")


class MappingError(ValueError):
    """The mapping is internally inconsistent and cannot be trusted."""


@dataclass
class SheetMapping:
    """The geometry of one learning-area sheet."""

    sheet_name: str

    #: Table 1 — item analysis.
    section_name_row: int
    examinee_count_row: int
    first_section_col: int
    last_section_col: int
    first_item_row: int
    last_item_row: int

    #: Table 2 — raw scores.
    first_score_row: int
    last_score_row: int

    #: Field name to cell reference, e.g. {"school": "B3"}.
    header: dict[str, str] = field(default_factory=dict)

    #: Least-mastered competencies, written down one column.
    least_mastered_column: str | None = None
    least_mastered_first_row: int | None = None
    least_mastered_count: int = 5

    #: Checkbox cells, keyed by the label printed beside them.
    intervention_activities: dict[str, str] = field(default_factory=dict)
    intervention_modes: dict[str, str] = field(default_factory=dict)
    intervention_other_cell: str | None = None

    signatories: dict[str, str] = field(default_factory=dict)

    #: The learning-area name found in the header, used to match our records.
    learning_area_hint: str | None = None

    #: Formula defects to repair on export: (wrong, right) applied to these rows.
    formula_repairs: list[tuple[str, str]] = field(default_factory=list)
    repair_rows: list[int] = field(default_factory=list)

    # --- derived capacity -------------------------------------------------
    @property
    def max_sections(self) -> int:
        return self.last_section_col - self.first_section_col + 1

    @property
    def item_capacity(self) -> int:
        return self.last_item_row - self.first_item_row + 1

    @property
    def max_examinees(self) -> int:
        return self.last_score_row - self.first_score_row + 1

    def section_column_letter(self, index: int) -> str:
        """Column letter for the nth section (zero-based)."""
        if not 0 <= index < self.max_sections:
            raise MappingError(f"This sheet holds {self.max_sections} sections; asked for #{index + 1}.")
        return get_column_letter(self.first_section_col + index)

    # --- validation -------------------------------------------------------
    def validate(self) -> list[str]:
        """Return the problems found; empty means the mapping is usable."""
        problems: list[str] = []

        if self.first_section_col < 1 or self.last_section_col < self.first_section_col:
            problems.append("Section columns are empty or reversed.")
        if self.last_item_row < self.first_item_row:
            problems.append("Item rows are reversed.")
        if self.last_score_row < self.first_score_row:
            problems.append("Score rows are reversed.")
        if self.item_capacity < 30:
            problems.append(f"Only {self.item_capacity} item rows; the shortest assessment has 30.")
        if self.max_sections < 1:
            problems.append("No section columns.")

        # The two tables must not overlap, or scores would overwrite frequencies.
        item_rows = range(self.first_item_row, self.last_item_row + 1)
        score_rows = range(self.first_score_row, self.last_score_row + 1)
        if max(item_rows.start, score_rows.start) <= min(item_rows.stop, score_rows.stop) - 1:
            problems.append("The item-analysis and raw-score blocks overlap.")

        if self.section_name_row in item_rows or self.section_name_row in score_rows:
            problems.append("The section-name row sits inside a data block.")
        if self.examinee_count_row in item_rows or self.examinee_count_row in score_rows:
            problems.append("The examinee-count row sits inside a data block.")

        if self.least_mastered_column is not None:
            try:
                column_index_from_string(self.least_mastered_column)
            except ValueError:
                problems.append(f"{self.least_mastered_column!r} is not a column letter.")
            if self.least_mastered_first_row is None:
                problems.append("A least-mastered column was given without a starting row.")

        return problems

    # --- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["formula_repairs"] = [list(pair) for pair in self.formula_repairs]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SheetMapping":
        payload = dict(data)
        payload["formula_repairs"] = [tuple(pair) for pair in payload.get("formula_repairs", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class TemplateMapping:
    """Every sheet in one uploaded workbook."""

    sheets: dict[str, SheetMapping] = field(default_factory=dict)
    #: "ai", "heuristic" or "manual" — recorded so a bad map can be traced.
    source: str = "heuristic"
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    version: int = 1

    def sheet_for(self, sheet_name: str) -> SheetMapping:
        mapping = self.sheets.get(sheet_name)
        if mapping is None:
            raise MappingError(f"The template has no mapped sheet named {sheet_name!r}.")
        return mapping

    def sheet_for_learning_area(self, name: str) -> SheetMapping | None:
        """Match a learning area to its sheet by the header value, then by tab name."""
        needle = name.strip().casefold()
        for mapping in self.sheets.values():
            if (mapping.learning_area_hint or "").strip().casefold() == needle:
                return mapping
        for mapping in self.sheets.values():
            if mapping.sheet_name.strip().casefold() == needle:
                return mapping
        return None

    def validate(self) -> dict[str, list[str]]:
        """Problems per sheet; an empty dict means the whole mapping is usable."""
        if not self.sheets:
            return {"*": ["The template has no learning-area sheets."]}
        return {
            name: problems
            for name, mapping in self.sheets.items()
            if (problems := mapping.validate())
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "confidence": self.confidence,
            "notes": list(self.notes),
            "sheets": {name: sheet.to_dict() for name, sheet in self.sheets.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateMapping":
        return cls(
            version=int(data.get("version", 1)),
            source=str(data.get("source", "heuristic")),
            confidence=float(data.get("confidence", 0.0)),
            notes=list(data.get("notes", [])),
            sheets={
                name: SheetMapping.from_dict(sheet)
                for name, sheet in (data.get("sheets") or {}).items()
            },
        )
