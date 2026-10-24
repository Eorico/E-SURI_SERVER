"""Assessment statistics, written once and reused everywhere.

Every figure the EQWART workbook and the dashboards show is computed here, with
the same rounding the template uses, so the API and the exported sheet always
agree (DRY). Pure functions over plain numbers — no ORM, no I/O, easy to test.
"""

from __future__ import annotations

import statistics as _stats
from dataclasses import dataclass, field

from api.models.enums import MasteryLevel
from utils.omr.mastery import proficient_cut_score, score_band

#: The template rounds percentages to two decimals (`ROUND(...,2)`).
PERCENT_DIGITS = 2

#: Share of papers in the upper and lower groups for the discrimination index.
DISCRIMINATION_GROUP_SHARE = 0.27


def percent(part: float, whole: float, digits: int = PERCENT_DIGITS) -> float:
    """Percentage, guarding division by zero the way `IFERROR` does."""
    if not whole:
        return 0.0
    return round((part / whole) * 100, digits)


def sample_stdev(values: list[float]) -> float:
    """Sample standard deviation, matching Excel's `STDEV`."""
    if len(values) < 2:
        return 0.0
    return _stats.stdev(values)


def mean_percentage_score(mean_score: float, item_count: int) -> float:
    """MPS = mean ÷ total items × 100 (EQWART rows 79 and 202)."""
    return percent(mean_score, item_count)


def rank_descending(values: list[float]) -> list[int]:
    """Excel `RANK(value, range, 0)`: highest value is rank 1, ties share a rank.

    The weakest item therefore carries the *largest* rank, which is how the
    template picks the five least-mastered competencies.
    """
    ordered = sorted(values, reverse=True)
    first_index = {}
    for position, value in enumerate(ordered, start=1):
        first_index.setdefault(value, position)
    return [first_index[value] for value in values]


def discrimination_index(scores_and_correctness: list[tuple[int, bool]]) -> float:
    """Upper/lower 27% discrimination index for one item.

    Takes (total score, was this item correct) per paper. Returns 0.0 when there
    are too few papers for the split to mean anything.
    """
    papers = len(scores_and_correctness)
    group_size = int(round(papers * DISCRIMINATION_GROUP_SHARE))
    if group_size < 1:
        return 0.0

    ordered = sorted(scores_and_correctness, key=lambda pair: pair[0], reverse=True)
    upper = sum(1 for _, correct in ordered[:group_size] if correct)
    lower = sum(1 for _, correct in ordered[-group_size:] if correct)
    return round((upper - lower) / group_size, 3)


@dataclass(frozen=True)
class ScoreSummary:
    """The "quiz statistics" panel, plus everything EQWART Table 2 needs."""

    paper_count: int
    item_count: int
    possible_points: int
    minimum: int
    maximum: int
    total: int
    mean: float
    median: float
    stdev: float
    mps: float
    at_or_above_75: int
    below_75: int
    band_counts: dict[MasteryLevel, int] = field(default_factory=dict)

    @property
    def minimum_percent(self) -> float:
        return percent(self.minimum, self.item_count)

    @property
    def maximum_percent(self) -> float:
        return percent(self.maximum, self.item_count)

    @property
    def mean_percent(self) -> float:
        return percent(self.mean, self.item_count)

    @property
    def median_percent(self) -> float:
        return percent(self.median, self.item_count)

    @property
    def stdev_percent(self) -> float:
        return percent(self.stdev, self.item_count)


def summarise_scores(scores: list[int], item_count: int) -> ScoreSummary:
    """Build the whole statistics block from a list of raw scores."""
    if not scores:
        return ScoreSummary(
            paper_count=0,
            item_count=item_count,
            possible_points=item_count,
            minimum=0,
            maximum=0,
            total=0,
            mean=0.0,
            median=0.0,
            stdev=0.0,
            mps=0.0,
            at_or_above_75=0,
            below_75=0,
            band_counts={level: 0 for level in MasteryLevel},
        )

    cut = proficient_cut_score(item_count)
    band_counts = {level: 0 for level in MasteryLevel}
    for score in scores:
        band_counts[score_band(score, item_count)] += 1

    mean = _stats.fmean(scores)
    return ScoreSummary(
        paper_count=len(scores),
        item_count=item_count,
        possible_points=item_count,
        minimum=min(scores),
        maximum=max(scores),
        total=sum(scores),
        mean=round(mean, 4),
        median=_stats.median(scores),
        stdev=round(sample_stdev([float(s) for s in scores]), 4),
        mps=mean_percentage_score(mean, item_count),
        at_or_above_75=sum(1 for score in scores if score >= cut),
        below_75=sum(1 for score in scores if score < cut),
        band_counts=band_counts,
    )
