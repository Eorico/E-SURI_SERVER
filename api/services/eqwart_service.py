"""EQWART report assembly and export.

Gathers one learning area's sections into the shape the workbook expects, keeps
the Coordinator's least-mastered competencies and intervention plan, and hands
the whole thing to the exporter.

Only approved submissions count, so a report never includes a paper still under
review.
"""

from __future__ import annotations

import uuid

from api.core.exceptions import ConflictError, NotFoundError, ValidationError
from api.models.enums import (
    LEAST_MASTERED_COUNT,
    MAX_SECTIONS_PER_LEARNING_AREA,
    AssessmentType,
    InterventionActivity,
    InterventionMode,
    ReportStatus,
)
from api.models.tables import EqwartReport, LeastMastered
from api.repositories.analysis_repository import (
    EqwartReportRepository,
    ItemAnalysisRepository,
    LeastMasteredRepository,
)
from api.repositories.assessment_repository import AssessmentRepository
from api.repositories.school_repository import LearningAreaRepository, SchoolRepository
from api.repositories.submission_repository import SubmissionRepository
from api.services.analysis_service import AnalysisService
import io as _io

from utils.omr.eqwart_export import EqwartExporter, EqwartSheetData, SectionColumn


class EqwartService:
    def __init__(
        self,
        reports: EqwartReportRepository,
        least_mastered: LeastMasteredRepository,
        learning_areas: LearningAreaRepository,
        schools: SchoolRepository,
        assessments: AssessmentRepository,
        submissions: SubmissionRepository,
        item_analysis: ItemAnalysisRepository,
        analysis: AnalysisService,
        templates,
    ):
        self.reports = reports
        self.least_mastered = least_mastered
        self.learning_areas = learning_areas
        self.schools = schools
        self.assessments = assessments
        self.submissions = submissions
        self.item_analysis = item_analysis
        self.analysis = analysis
        #: Supplies the active, administrator-approved template and its mapping.
        self.templates = templates

    # --- report lifecycle -------------------------------------------------
    def get_or_create(
        self, learning_area_id: uuid.UUID, assessment_type: AssessmentType, school_year: str
    ) -> EqwartReport:
        if self.learning_areas.get(learning_area_id) is None:
            raise NotFoundError("That learning area does not exist.")

        existing = self.reports.find(learning_area_id, assessment_type, school_year)
        if existing is not None:
            return existing

        report = self.reports.add(
            EqwartReport(
                learning_area_id=learning_area_id,
                assessment_type=assessment_type,
                school_year=school_year,
                status=ReportStatus.draft,
            )
        )
        self._seed_least_mastered(report)
        return report

    def _seed_least_mastered(self, report: EqwartReport) -> None:
        """Pre-fill the five slots with the weakest items, ready for encoding."""
        columns = self._columns_for(report)
        if not columns:
            return

        totals: dict[int, int] = {}
        for _, frequencies in columns:
            for index, frequency in enumerate(frequencies, start=1):
                totals[index] = totals.get(index, 0) + frequency

        weakest = sorted(totals.items(), key=lambda pair: (pair[1], pair[0]))[:LEAST_MASTERED_COUNT]
        self.least_mastered.add_all(
            [
                LeastMastered(report_id=report.id, position=position, item_no=item_no)
                for position, (item_no, _) in enumerate(weakest, start=1)
            ]
        )

    def get_report(self, report_id: uuid.UUID) -> EqwartReport:
        return self._require_report(report_id)

    def set_least_mastered(
        self,
        report_id: uuid.UUID,
        *,
        position: int,
        competency_text: str,
        competency_id: uuid.UUID | None = None,
    ) -> LeastMastered:
        """The Coordinator encodes the competency behind a weak item."""
        report = self._require_report(report_id)
        self._guard_editable(report)

        if not 1 <= position <= LEAST_MASTERED_COUNT:
            raise ValidationError(f"Position must be between 1 and {LEAST_MASTERED_COUNT}.")

        existing = self.least_mastered.at_position(report_id, position)
        if existing is None:
            return self.least_mastered.add(
                LeastMastered(
                    report_id=report_id,
                    position=position,
                    competency_text=competency_text,
                    competency_id=competency_id,
                )
            )
        return self.least_mastered.update(
            existing, competency_text=competency_text, competency_id=competency_id
        )

    def set_intervention(
        self,
        report_id: uuid.UUID,
        *,
        activities: list[InterventionActivity],
        mode: InterventionMode,
        other: str | None = None,
    ) -> EqwartReport:
        report = self._require_report(report_id)
        self._guard_editable(report)

        if InterventionActivity.other in activities and not other:
            raise ValidationError("Describe the other intervention you plan to use.")

        return self.reports.update(
            report,
            intervention_activities=[activity.value for activity in activities],
            intervention_mode=mode,
            intervention_other=other,
        )

    def set_signatories(
        self,
        report_id: uuid.UUID,
        *,
        key_teacher_name: str | None = None,
        reviewed_by_name: str | None = None,
        approved_by_name: str | None = None,
    ) -> EqwartReport:
        report = self._require_report(report_id)
        changes = {
            key: value
            for key, value in (
                ("key_teacher_name", key_teacher_name),
                ("reviewed_by_name", reviewed_by_name),
                ("approved_by_name", approved_by_name),
            )
            if value is not None
        }
        return self.reports.update(report, **changes) if changes else report

    def advance_status(self, report_id: uuid.UUID, status: ReportStatus) -> EqwartReport:
        report = self._require_report(report_id)
        return self.reports.update(report, status=status)

    # --- export -----------------------------------------------------------
    def export(self, report_ids: list[uuid.UUID]) -> bytes:
        reports = [self._require_report(rid) for rid in report_ids]
        if not reports:
            raise ValidationError("Select at least one learning area to export.")
        school_year = reports[0].school_year
        return self._write(school_year, [self.build_sheet(report) for report in reports])

    def export_school_year(self, school_year: str) -> bytes:
        reports = self.reports.for_school_year(school_year)
        if not reports:
            raise NotFoundError(f"No EQWART reports exist for {school_year}.")
        return self._write(school_year, [self.build_sheet(report) for report in reports])

    def _write(self, school_year: str, sheets: list[EqwartSheetData]) -> bytes:
        """Fill the template an administrator activated for this school year.

        The mapping travels with the template, so a reissued DepEd workbook
        exports correctly without any code change here.
        """
        workbook, mapping = self.templates.active_mapping(school_year)
        placed = [sheet for sheet in sheets if self._retarget(sheet, mapping)]
        if not placed:
            raise ValidationError(
                "None of these learning areas match a sheet in the active template."
            )
        return EqwartExporter(_io.BytesIO(workbook), mapping).export(placed)

    @staticmethod
    def _retarget(sheet: EqwartSheetData, mapping) -> bool:
        """Point the sheet at whatever tab this template uses for that subject."""
        plan = mapping.sheets.get(sheet.sheet_name) or mapping.sheet_for_learning_area(
            sheet.learning_area
        )
        if plan is None:
            return False
        sheet.sheet_name = plan.sheet_name
        return True

    def build_sheet(self, report: EqwartReport) -> EqwartSheetData:
        area = self.learning_areas.get(report.learning_area_id)
        if area is None:
            raise NotFoundError("That learning area does not exist.")
        school = self.schools.current()

        sections: list[SectionColumn] = []
        item_count = 0
        for assessment, frequencies in self._columns_for(report):
            item_count = assessment.item_count
            papers = self.submissions.approved_for_assessment(assessment.id)
            sections.append(
                SectionColumn(
                    name=assessment.class_.section.name,
                    examinee_count=len(papers),
                    item_frequencies=frequencies,
                    raw_scores=[paper.score or 0 for paper in papers],
                )
            )

        if len(sections) > MAX_SECTIONS_PER_LEARNING_AREA:
            raise ConflictError(
                f"The workbook holds {MAX_SECTIONS_PER_LEARNING_AREA} sections; "
                f"{len(sections)} have results."
            )

        rows = self.least_mastered.for_report(report.id)
        return EqwartSheetData(
            sheet_name=area.sheet_name,
            division=(school.division if school else "") or "",
            cluster=(school.cluster if school else "") or "",
            school=(school.name if school else "") or "",
            school_year=report.school_year,
            learning_area=area.name,
            grade_level=area.grade_level,
            item_count=item_count or 60,
            sections=sections,
            least_mastered=[row.competency_text or "" for row in rows],
            intervention_activities=list(report.intervention_activities or []),
            intervention_other=report.intervention_other,
            intervention_mode=(
                report.intervention_mode.value if report.intervention_mode else None
            ),
            key_teacher=report.key_teacher_name,
            reviewed_by=report.reviewed_by_name,
            approved_by=report.approved_by_name,
        )

    # --- helpers ----------------------------------------------------------
    def _columns_for(self, report: EqwartReport):
        """(assessment, per-item correct counts) for each section, in order."""
        assessments = self.assessments.for_learning_area(
            report.learning_area_id, report.assessment_type, report.school_year
        )
        assessments.sort(key=lambda a: (a.class_.section.position, a.class_.section.name))

        columns = []
        for assessment in assessments:
            rows = self.item_analysis.for_assessment(assessment.id)
            if not rows:
                rows = self.analysis.recompute(assessment.id)
            by_item = {row.item_no: row.correct_count for row in rows}
            columns.append(
                (assessment, [by_item.get(item, 0) for item in range(1, assessment.item_count + 1)])
            )
        return columns

    def _require_report(self, report_id: uuid.UUID) -> EqwartReport:
        report = self.reports.get(report_id)
        if report is None:
            raise NotFoundError("That report does not exist.")
        return report

    @staticmethod
    def _guard_editable(report: EqwartReport) -> None:
        if report.status is ReportStatus.approved:
            raise ConflictError("That report is approved and can no longer be edited.")
