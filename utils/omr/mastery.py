"""Proficiency bands and instructional assistance — one source of truth.

Mirrors the EQWART template: the percent thresholds are column Z, the raw-score
bands are rows 194-198, and the assistance mapping is column AA. The analysis
service and the workbook export both read from here so the two can never
disagree (DRY).
"""

from __future__ import annotations

from dataclasses import dataclass

from api.models.enums import InstructionalAssistance, MasteryLevel


@dataclass(frozen=True)
class Band:
    """A proficiency band, expressed as an inclusive upper bound."""

    level: MasteryLevel
    assistance: InstructionalAssistance
    #: Upper bound for an item's percent correct (EQWART column Z).
    percent_ceiling: float
    #: Upper bound as a share of the item count, for raw scores (rows 194-198).
    score_share_ceiling: float


#: Ordered weakest to strongest. `None` ceilings mean "everything above".
BANDS: tuple[Band, ...] = (
    Band(MasteryLevel.not_proficient, InstructionalAssistance.reteach, 24.0, 0.25),
    Band(MasteryLevel.low_proficient, InstructionalAssistance.remediation, 49.0, 0.50),
    Band(MasteryLevel.nearly_proficient, InstructionalAssistance.intervention, 74.0, 0.75),
    Band(MasteryLevel.proficient, InstructionalAssistance.enhancement, 89.0, 0.90),
    Band(MasteryLevel.highly_proficient, InstructionalAssistance.advancement, 100.0, 1.00),
)


def mastery_level(percent: float) -> MasteryLevel:
    """Level of mastery for an item's percent correct (EQWART column Z)."""
    for band in BANDS:
        if percent <= band.percent_ceiling:
            return band.level
    return MasteryLevel.highly_proficient


def instructional_assistance(level: MasteryLevel) -> InstructionalAssistance:
    """The assistance the template pairs with a level (EQWART column AA)."""
    for band in BANDS:
        if band.level is level:
            return band.assistance
    return InstructionalAssistance.advancement


def score_band(score: int, item_count: int) -> MasteryLevel:
    """Band for a learner's raw score.

    For 60 items this reproduces the template's legend exactly:
    0-15, 16-30, 31-45, 46-54, 55-60.
    """
    if item_count <= 0:
        return MasteryLevel.not_proficient
    share = score / item_count
    for band in BANDS:
        if share <= band.score_share_ceiling:
            return band.level
    return MasteryLevel.highly_proficient


def score_band_bounds(item_count: int) -> dict[MasteryLevel, tuple[int, int]]:
    """Inclusive raw-score range per band, for the legend and dashboards."""
    bounds: dict[MasteryLevel, tuple[int, int]] = {}
    lower = 0
    for band in BANDS:
        upper = int(band.score_share_ceiling * item_count)
        upper = min(upper, item_count)
        bounds[band.level] = (lower, upper)
        lower = upper + 1
    return bounds


def proficient_cut_score(item_count: int) -> int:
    """Lowest score counted as "75% and above".

    The template uses the Proficient cut (`>= 46` of 60), not a literal 75%.
    """
    nearly = next(band for band in BANDS if band.level is MasteryLevel.nearly_proficient)
    return int(nearly.score_share_ceiling * item_count) + 1
