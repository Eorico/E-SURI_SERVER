"""AI-assisted template mapping.

An administrator uploads a workbook; this turns it into a `TemplateMapping`.

What the model sees is a **structure digest** — sheet names, printed labels,
formula shapes, merged ranges and a count of how many numeric cells sit in each
row. It never sees a learner name or a score, so uploading a template carries
no student data off the server.

The model only ever *proposes*. Every proposal is schema-checked, validated and
dry-run against the real workbook before it can be stored, and the heuristic
analyser is used whenever the AI is unavailable or proposes something worse.
That keeps the system working with no API key at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openpyxl.workbook.workbook import Workbook

from api.core.config import settings
from api.core.logging import get_logger
from utils.omr.template_analyzer import HeuristicAnalyzer, build_digest
from utils.omr.template_mapping import SheetMapping, TemplateMapping

logger = get_logger(__name__)

#: Keep the prompt bounded regardless of how large a workbook is.
MAX_LABELS_SENT = 160
MAX_FORMULAS_SENT = 40
MAX_STRUCTURAL_COLUMN = 12

SYSTEM_PROMPT = """\
You map the layout of a Philippine DepEd item-analysis workbook (EQWART/ETWART).

You are given a structure digest of ONE worksheet: its printed labels with cell
references, a sample of its formulas, merged ranges, and how many numeric cells
appear on each row. You never see student data.

Identify where each piece of information belongs and return JSON only.

The sheet always has two stacked tables:
  Table 1 - item analysis. One column per section/class. One row per test item,
            holding the COUNT of correct responses for that item in that section.
  Table 2 - raw scores. The same section columns. One row per examinee, holding
            that examinee's total score.

Between and below them there are usually: a header block (division, cluster,
school, school year, learning area, grade level, number of items), a
"least mastered competencies" list the coordinator fills in, intervention
checkboxes, and signatory name lines.

Rules:
- Rows and columns are 1-based. Columns in `header`, `least_mastered_column`
  and the checkbox maps are spreadsheet references like "B3" or "E".
- `first_section_col`/`last_section_col` bound the section columns only. Exclude
  any "Total", "Overall" or statistics column.
- `first_item_row`/`last_item_row` cover ONLY item rows, stopping before any
  SUM/MEAN/SD/MPS summary row.
- `first_score_row`/`last_score_row` cover ONLY examinee rows.
- A header field whose label you cannot find must be omitted, not guessed.
- The two tables must not overlap.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "section_name_row",
        "examinee_count_row",
        "first_section_col",
        "last_section_col",
        "first_item_row",
        "last_item_row",
        "first_score_row",
        "last_score_row",
        "header",
        "least_mastered_column",
        "least_mastered_first_row",
        "least_mastered_count",
        "intervention_activities",
        "intervention_modes",
        "intervention_other_cell",
        "signatories",
        "learning_area_hint",
        "confidence",
    ],
    "properties": {
        "section_name_row": {"type": "integer"},
        "examinee_count_row": {"type": "integer"},
        "first_section_col": {"type": "integer"},
        "last_section_col": {"type": "integer"},
        "first_item_row": {"type": "integer"},
        "last_item_row": {"type": "integer"},
        "first_score_row": {"type": "integer"},
        "last_score_row": {"type": "integer"},
        "header": {"type": "object", "additionalProperties": {"type": "string"}},
        "least_mastered_column": {"type": ["string", "null"]},
        "least_mastered_first_row": {"type": ["integer", "null"]},
        "least_mastered_count": {"type": "integer"},
        "intervention_activities": {"type": "object", "additionalProperties": {"type": "string"}},
        "intervention_modes": {"type": "object", "additionalProperties": {"type": "string"}},
        "intervention_other_cell": {"type": ["string", "null"]},
        "signatories": {"type": "object", "additionalProperties": {"type": "string"}},
        "learning_area_hint": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
}


class MapperGateway(Protocol):
    """Proposes a mapping for one sheet digest. Returns {} when unavailable."""

    @property
    def available(self) -> bool: ...
    def propose(self, sheet_digest: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class NullMapperGateway:
    """Used when no API key is configured; the heuristic then stands alone."""

    @property
    def available(self) -> bool:
        return False

    def propose(self, sheet_digest: dict[str, Any]) -> dict[str, Any]:
        return {}


class OpenAIMapperGateway:
    """Asks an OpenAI model to read one sheet digest."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._model = model or settings.OPENAI_MODEL
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI  # deferred: optional dependency

            self._client = OpenAI(api_key=self._api_key, timeout=settings.OPENAI_TIMEOUT_SECONDS)
        return self._client

    def propose(self, sheet_digest: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            return {}
        try:
            response = self._ensure_client().chat.completions.create(
                model=self._model,
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "sheet_mapping",
                        "strict": True,
                        "schema": RESPONSE_SCHEMA,
                    },
                },
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(sheet_digest, ensure_ascii=False)},
                ],
            )
            return json.loads(response.choices[0].message.content or "{}")
        except Exception:
            # A mapping failure must never block an upload; the heuristic covers it.
            logger.exception("openai_template_mapping_failed", extra={"model": self._model})
            return {}


@dataclass(frozen=True)
class MappingProposal:
    mapping: TemplateMapping
    #: "ai", "heuristic", or "ai+heuristic" when the two were combined.
    source: str
    problems: dict[str, list[str]]


class TemplateMapperService:
    """Produces the best mapping it can, and says where it came from.

    Sheets that share a layout are mapped once and the result reused, so a
    nine-sheet workbook with two distinct layouts costs two model calls.
    """

    def __init__(self, gateway: MapperGateway | None = None, analyzer: HeuristicAnalyzer | None = None):
        self.gateway = gateway or NullMapperGateway()
        self.analyzer = analyzer or HeuristicAnalyzer()

    def map_workbook(self, workbook: Workbook) -> MappingProposal:
        baseline = self.analyzer.analyze(workbook)
        if not self.gateway.available:
            return MappingProposal(baseline, "heuristic", baseline.validate())

        digest = build_digest(workbook)
        by_name = {sheet["name"]: sheet for sheet in digest["sheets"]}

        sheets: dict[str, SheetMapping] = {}
        used_ai = False
        cache: dict[str, SheetMapping | None] = {}

        for name, heuristic_sheet in baseline.sheets.items():
            sheet_digest = by_name.get(name)
            if sheet_digest is None:
                sheets[name] = heuristic_sheet
                continue

            signature = _layout_signature(sheet_digest)
            if signature not in cache:
                cache[signature] = self._propose_sheet(name, sheet_digest)

            proposed = cache[signature]
            chosen = self._better(proposed, heuristic_sheet, name)
            used_ai = used_ai or chosen is proposed
            sheets[name] = chosen

        mapping = TemplateMapping(
            sheets=sheets,
            source="ai" if used_ai else "heuristic",
            confidence=max(baseline.confidence, 0.95 if used_ai else baseline.confidence),
            notes=baseline.notes,
            version=1,
        )
        source = "ai+heuristic" if used_ai and len(sheets) != sum(
            1 for name in sheets if sheets[name] is not baseline.sheets.get(name)
        ) else mapping.source
        return MappingProposal(mapping, source, mapping.validate())

    # --- internals --------------------------------------------------------
    def _propose_sheet(self, name: str, sheet_digest: dict[str, Any]) -> SheetMapping | None:
        payload = self.gateway.propose(_trim(sheet_digest))
        if not payload:
            return None
        try:
            return SheetMapping(
                sheet_name=name,
                section_name_row=int(payload["section_name_row"]),
                examinee_count_row=int(payload["examinee_count_row"]),
                first_section_col=int(payload["first_section_col"]),
                last_section_col=int(payload["last_section_col"]),
                first_item_row=int(payload["first_item_row"]),
                last_item_row=int(payload["last_item_row"]),
                first_score_row=int(payload["first_score_row"]),
                last_score_row=int(payload["last_score_row"]),
                header=dict(payload.get("header") or {}),
                least_mastered_column=payload.get("least_mastered_column"),
                least_mastered_first_row=payload.get("least_mastered_first_row"),
                least_mastered_count=int(payload.get("least_mastered_count") or 5),
                intervention_activities=dict(payload.get("intervention_activities") or {}),
                intervention_modes=dict(payload.get("intervention_modes") or {}),
                intervention_other_cell=payload.get("intervention_other_cell"),
                signatories=dict(payload.get("signatories") or {}),
                learning_area_hint=payload.get("learning_area_hint"),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("ai_mapping_unusable", extra={"sheet": name})
            return None

    @staticmethod
    def _better(
        proposed: SheetMapping | None, heuristic: SheetMapping, name: str
    ) -> SheetMapping:
        """Prefer the AI only when it validates and finds at least as much."""
        if proposed is None or proposed.validate():
            return heuristic
        if heuristic.validate():
            return _renamed(proposed, name)

        def richness(sheet: SheetMapping) -> int:
            return (
                len(sheet.header)
                + len(sheet.intervention_activities)
                + len(sheet.intervention_modes)
                + len(sheet.signatories)
                + (1 if sheet.least_mastered_column else 0)
            )

        return _renamed(proposed, name) if richness(proposed) > richness(heuristic) else heuristic


def _renamed(sheet: SheetMapping, name: str) -> SheetMapping:
    sheet.sheet_name = name
    return sheet


def _trim(sheet_digest: dict[str, Any]) -> dict[str, Any]:
    """Shrink a digest to the structural essentials before sending it."""
    labels = [
        label
        for label in sheet_digest.get("labels", [])
        if _column_index(label["cell"]) <= MAX_STRUCTURAL_COLUMN
    ][:MAX_LABELS_SENT]

    return {
        "name": sheet_digest["name"],
        "max_row": sheet_digest.get("max_row"),
        "max_column": sheet_digest.get("max_column"),
        "labels": labels,
        "formulas": sheet_digest.get("formulas", [])[:MAX_FORMULAS_SENT],
        "numeric_density": _runs(sheet_digest.get("numeric_density", [])),
        "merged": sheet_digest.get("merged", [])[:30],
    }


def _runs(density: list[dict[str, int]]) -> list[dict[str, int]]:
    """Collapse per-row counts into contiguous blocks, which is what matters."""
    runs: list[dict[str, int]] = []
    for entry in density:
        if runs and entry["row"] == runs[-1]["to_row"] + 1 and entry["from_col"] == runs[-1]["from_col"]:
            runs[-1]["to_row"] = entry["row"]
            runs[-1]["rows"] += 1
        else:
            runs.append(
                {
                    "from_row": entry["row"],
                    "to_row": entry["row"],
                    "rows": 1,
                    "from_col": entry["from_col"],
                    "to_col": entry["to_col"],
                }
            )
    return runs[:40]


def _layout_signature(sheet_digest: dict[str, Any]) -> str:
    """Sheets printing the same left-hand labels share a layout."""
    anchors = [
        f"{label['cell']}={label['text'][:24]}"
        for label in sheet_digest.get("labels", [])
        if _column_index(label["cell"]) <= 2
    ]
    return "|".join(anchors[:60])


def _column_index(cell: str) -> int:
    from openpyxl.utils import column_index_from_string

    letters = "".join(ch for ch in cell if ch.isalpha())
    try:
        return column_index_from_string(letters)
    except ValueError:
        return 999
