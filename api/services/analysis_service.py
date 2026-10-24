"""Item analysis.

Turns approved submissions into the per-item figures the EQWART sheet and the
teacher dashboards show: correct count, percent correct, rank, discrimination
index, level of mastery and instructional assistance.

Bands and thresholds come from `utils.omr.mastery`, and every statistic from
`utils.omr.statistics`, so this service never reimplements a formula (DRY).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from api.core.exceptions import NotFoundError
from api.models.enums import LEAST_MASTERED_COUNT, ResponseFlag
from api.models.tables import Assessment, ItemAnalysis, Response, Submission
from api.repositories.analysis_repository import ItemAnalysisRepository
from api.repositories.assessment_repository import AnswerKeyRepository, AssessmentRepository
from api.repositories.submission_repository import SubmissionRepository
from utils.omr.mastery import instructional_assistance, mastery_level
from utils.omr.statistics import (
    ScoreSummary,
    discrimination_index,
    percent,
    rank_descending,
    summarise_scores,
)

#: How a blank response appears in the distribution, matching the reference tool.
BLANK_KEY = "_"


@dataclass(frozen=True)
class ItemFigures:
    """One row of the item-analysis table, before it is persisted."""

    item_no: int
    correct_option: str
    correct_count: int
    attempted_count: int
    percent_correct: float
    rank: int
    discrimination_index: float
    distribution: dict[str, int]


class AnalysisService:
    """Computes and stores item analysis for one assessment."""

    def __init__(
        self,
        assessments: AssessmentRepository,
        submissions: SubmissionRepository,
        answer_keys: AnswerKeyRepository,
        item_analysis: ItemAnalysisRepository,
    ):
        self.assessments = assessments
        self.submissions = submissions
        self.answer_keys = answer_keys
        self.item_analysis = item_analysis

    # --- public API -------------------------------------------------------
    def recompute(self, assessment_id: uuid.UUID) -> list[ItemAnalysis]:
        """Recalculate from scratch and replace the stored rows."""
        assessment = self._require(assessment_id)
        key = self.answer_keys.as_map(assessment_id)
        papers = self.submissions.approved_with_responses(assessment_id)

        figures = self.compute(assessment, key, papers)
        rows = [
            ItemAnalysis(
                assessment_id=assessment_id,
                item_no=figure.item_no,
                correct_count=figure.correct_count,
                attempted_count=figure.attempted_count,
                percent_correct=figure.percent_correct,
                rank=figure.rank,
                discrimination_index=figure.discrimination_index,
                mastery_level=mastery_level(figure.percent_correct),
                instructional_assistance=instructional_assistance(
                    mastery_level(figure.percent_correct)
                ),
                distribution=figure.distribution,
            )
            for figure in figures
        ]
        return self.item_analysis.replace_for_assessment(assessment_id, rows)

    def compute(
        self,
        assessment: Assessment,
        key: dict[int, str],
        papers: list[Submission],
    ) -> list[ItemFigures]:
        """Pure computation, so it can be tested without a database."""
        paper_count = len(papers)
        totals = {paper.id: (paper.score or 0) for paper in papers}

        correct_by_item: dict[int, int] = {}
        attempted_by_item: dict[int, int] = {}
        distribution: dict[int, dict[str, int]] = {}
        correctness: dict[int, list[tuple[int, bool]]] = {}

        for item_no in range(1, assessment.item_count + 1):
            correct_by_item[item_no] = 0
            attempted_by_item[item_no] = 0
            distribution[item_no] = {}
            correctness[item_no] = []

        for paper in papers:
            for response in paper.responses:
                item_no = response.item_no
                if item_no not in correct_by_item:
                    continue

                attempted_by_item[item_no] += 1
                if response.is_correct:
                    correct_by_item[item_no] += 1

                bucket = self._distribution_key(response)
                distribution[item_no][bucket] = distribution[item_no].get(bucket, 0) + 1
                correctness[item_no].append((totals[paper.id], response.is_correct))

        percents = [
            percent(correct_by_item[item], paper_count) if paper_count else 0.0
            for item in range(1, assessment.item_count + 1)
        ]
        ranks = rank_descending(percents)

        return [
            ItemFigures(
                item_no=item_no,
                correct_option=key.get(item_no, ""),
                correct_count=correct_by_item[item_no],
                attempted_count=attempted_by_item[item_no],
                percent_correct=percents[index],
                rank=ranks[index],
                discrimination_index=discrimination_index(correctness[item_no]),
                distribution=distribution[item_no],
            )
            for index, item_no in enumerate(range(1, assessment.item_count + 1))
        ]

    def summary(self, assessment_id: uuid.UUID) -> ScoreSummary:
        """The statistics panel: counts, min/max, mean, median, SD, MPS."""
        assessment = self._require(assessment_id)
        scores = [
            paper.score or 0 for paper in self.submissions.approved_for_assessment(assessment_id)
        ]
        return summarise_scores(scores, assessment.item_count)

    def least_mastered(
        self, assessment_id: uuid.UUID, limit: int = LEAST_MASTERED_COUNT
    ) -> list[ItemAnalysis]:
        """The weakest items, which the Coordinator maps to competencies."""
        rows = self.item_analysis.for_assessment(assessment_id)
        if not rows:
            rows = self.recompute(assessment_id)
        return sorted(rows, key=lambda row: (row.percent_correct or 0.0, row.item_no))[:limit]

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _distribution_key(response: Response) -> str:
        """Blank is `_`; several marks are joined, e.g. `CD`."""
        if response.flag is ResponseFlag.blank or not response.marked_option:
            return BLANK_KEY
        return response.marked_option

    def _require(self, assessment_id: uuid.UUID) -> Assessment:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise NotFoundError("That assessment does not exist.")
        return assessment
