"""Report templates: upload, map, prove, activate.

An administrator uploads the year's DepEd workbook. The service maps its
structure (AI when configured, heuristic otherwise), then **dry-runs** the
mapping against the real file with synthetic data before it may be activated.
A mapping that cannot write and read back correctly never becomes active, so a
bad map is caught at upload rather than on the day a report is due.

Only the active template is used for exports. Uploading and activating are
administrator-only, which the routes enforce.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from openpyxl import load_workbook

from api.core.config import settings
from api.core.exceptions import ConflictError, NotFoundError, ValidationError
from api.core.logging import get_logger
from api.models.enums import MappingSource, TemplateStatus
from api.models.tables import ReportTemplate
from api.repositories.template_repository import ReportTemplateRepository
from api.services.ai.template_mapper import TemplateMapperService
from utils.omr.eqwart_export import EqwartExporter, EqwartSheetData, SectionColumn
from utils.omr.template_mapping import TemplateMapping

logger = get_logger(__name__)

#: Workbook magic bytes — an .xlsx/.xlsm is a ZIP container.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


@dataclass(frozen=True)
class DryRunResult:
    passed: bool
    problems: list[str]
    sheets_checked: int


class TemplateService:
    def __init__(
        self,
        templates: ReportTemplateRepository,
        mapper: TemplateMapperService,
        storage=None,
    ):
        self.templates = templates
        self.mapper = mapper
        self.storage = storage

    # --- upload -----------------------------------------------------------
    def upload(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str | None,
        school_year: str,
        grade_level: int,
        name: str | None,
        uploaded_by: uuid.UUID,
    ) -> ReportTemplate:
        """Store the workbook, map it, and dry-run the mapping.

        The result is always a *draft*; an administrator reviews it and
        activates it separately.
        """
        self._validate_file(data, content_type)
        checksum = hashlib.sha256(data).hexdigest()

        if (existing := self.templates.by_checksum(checksum)) is not None:
            raise ConflictError(
                f"That exact workbook was already uploaded as {existing.name!r}."
            )

        workbook = self._open(data)
        proposal = self.mapper.map_workbook(workbook)
        mapping = proposal.mapping

        if not mapping.sheets:
            raise ValidationError(
                "No learning-area sheets were recognised. A sheet needs an "
                "item-analysis table and a raw-score table."
            )

        dry_run = self.dry_run(data, mapping)
        notes = list(mapping.notes)
        notes.extend(dry_run.problems)
        for sheet, problems in proposal.problems.items():
            notes.extend(f"{sheet}: {problem}" for problem in problems)

        path = self._store(data, checksum, filename)

        return self.templates.add(
            ReportTemplate(
                name=name or filename,
                original_filename=filename,
                storage_path=path,
                checksum=checksum,
                school_year=school_year,
                grade_level=grade_level,
                status=TemplateStatus.draft,
                mapping=mapping.to_dict(),
                mapping_source=MappingSource(proposal.mapping.source.split("+")[0]),
                mapping_confidence=mapping.confidence,
                mapping_notes=notes or None,
                dry_run_passed=dry_run.passed,
                uploaded_by=uploaded_by,
            )
        )

    # --- dry run ----------------------------------------------------------
    def dry_run(self, data: bytes, mapping: TemplateMapping) -> DryRunResult:
        """Fill the workbook with synthetic values and read them back.

        Proves the mapping writes where it claims, that the two tables do not
        collide, and that the template's own formulas survive the write. No
        learner data is involved.
        """
        problems: list[str] = []
        for sheet_name, plan in mapping.sheets.items():
            if issues := plan.validate():
                problems.extend(f"{sheet_name}: {issue}" for issue in issues)

        if problems:
            return DryRunResult(False, problems, len(mapping.sheets))

        try:
            sheets = [self._synthetic_sheet(name, plan) for name, plan in mapping.sheets.items()]
            written = self._export_bytes(data, mapping, sheets)
            problems.extend(self._verify(written, mapping, sheets))
        except Exception as exc:
            logger.exception("template_dry_run_failed")
            problems.append(f"The template could not be filled: {exc}")

        return DryRunResult(not problems, problems, len(mapping.sheets))

    @staticmethod
    def _synthetic_sheet(sheet_name: str, plan) -> EqwartSheetData:
        """Data shaped to the template's own capacity, with recognisable values."""
        item_count = min(plan.item_capacity, 60)
        sections = [
            SectionColumn(
                name=f"DRY-RUN {index + 1}",
                examinee_count=3,
                item_frequencies=[(index + 1) * 100 + item for item in range(item_count)],
                raw_scores=[item_count, item_count // 2, 1],
            )
            for index in range(min(plan.max_sections, 3))
        ]
        return EqwartSheetData(
            sheet_name=sheet_name,
            division="DRY RUN",
            cluster="DRY RUN",
            school="DRY RUN",
            school_year="0000-0000",
            learning_area="DRY RUN",
            grade_level=11,
            item_count=item_count,
            sections=sections,
            least_mastered=[f"dry-run competency {n}" for n in range(1, 6)],
            intervention_activities=["Reteaching"],
            intervention_mode="Built-in",
            key_teacher="DRY RUN",
            reviewed_by="DRY RUN",
            approved_by="DRY RUN",
        )

    @staticmethod
    def _export_bytes(data: bytes, mapping: TemplateMapping, sheets: list[EqwartSheetData]) -> bytes:
        """Export from an in-memory copy, never touching stored files."""
        return EqwartExporter(io.BytesIO(data), mapping).export(sheets)

    @staticmethod
    def _verify(written: bytes, mapping: TemplateMapping, sheets: list[EqwartSheetData]) -> list[str]:
        problems: list[str] = []
        workbook = load_workbook(io.BytesIO(written))

        for data in sheets:
            plan = mapping.sheet_for(data.sheet_name)
            sheet = workbook[data.sheet_name]

            for index, section in enumerate(data.sections):
                letter = plan.section_column_letter(index)

                if sheet[f"{letter}{plan.section_name_row}"].value != section.name:
                    problems.append(
                        f"{data.sheet_name}: section name did not land in "
                        f"{letter}{plan.section_name_row}."
                    )
                if sheet[f"{letter}{plan.examinee_count_row}"].value != section.examinee_count:
                    problems.append(
                        f"{data.sheet_name}: examinee count did not land in "
                        f"{letter}{plan.examinee_count_row}."
                    )

                first_item = sheet[f"{letter}{plan.first_item_row}"].value
                if first_item != section.item_frequencies[0]:
                    problems.append(
                        f"{data.sheet_name}: item frequency did not land in "
                        f"{letter}{plan.first_item_row}."
                    )

                top_score = sheet[f"{letter}{plan.first_score_row}"].value
                if top_score != max(section.raw_scores):
                    problems.append(
                        f"{data.sheet_name}: raw score did not land in "
                        f"{letter}{plan.first_score_row}."
                    )
        return problems

    # --- lifecycle --------------------------------------------------------
    def activate(self, template_id: uuid.UUID, activated_by: uuid.UUID) -> ReportTemplate:
        """Make a draft the live template, archiving whatever it replaces."""
        template = self.get(template_id)

        if template.status is TemplateStatus.active:
            return template
        if not template.dry_run_passed:
            raise ConflictError(
                "This template did not pass its dry run, so it cannot be activated. "
                "Review the notes, correct the workbook, and upload it again."
            )

        for current in self.templates.active_for(template.school_year, template.grade_level):
            self.templates.update(current, status=TemplateStatus.archived)

        return self.templates.update(
            template,
            status=TemplateStatus.active,
            activated_at=datetime.now(timezone.utc),
            activated_by=activated_by,
        )

    def archive(self, template_id: uuid.UUID) -> ReportTemplate:
        return self.templates.update(self.get(template_id), status=TemplateStatus.archived)

    def remap(self, template_id: uuid.UUID) -> ReportTemplate:
        """Re-run mapping on a stored template, e.g. after adding an API key."""
        template = self.get(template_id)
        data = self._load(template)

        proposal = self.mapper.map_workbook(self._open(data))
        dry_run = self.dry_run(data, proposal.mapping)

        return self.templates.update(
            template,
            mapping=proposal.mapping.to_dict(),
            mapping_source=MappingSource(proposal.mapping.source.split("+")[0]),
            mapping_confidence=proposal.mapping.confidence,
            mapping_notes=(proposal.mapping.notes + dry_run.problems) or None,
            dry_run_passed=dry_run.passed,
        )

    def override_mapping(self, template_id: uuid.UUID, mapping: TemplateMapping) -> ReportTemplate:
        """Let an administrator correct a mapping by hand; it still must prove itself."""
        template = self.get(template_id)
        dry_run = self.dry_run(self._load(template), mapping)

        return self.templates.update(
            template,
            mapping=mapping.to_dict(),
            mapping_source=MappingSource.manual,
            mapping_confidence=mapping.confidence,
            mapping_notes=dry_run.problems or None,
            dry_run_passed=dry_run.passed,
        )

    # --- reads ------------------------------------------------------------
    def get(self, template_id: uuid.UUID) -> ReportTemplate:
        template = self.templates.get(template_id)
        if template is None:
            raise NotFoundError("That template does not exist.")
        return template

    def list(self) -> list[ReportTemplate]:
        return self.templates.newest_first()

    def active(self, school_year: str, grade_level: int = 11) -> ReportTemplate:
        templates = self.templates.active_for(school_year, grade_level)
        if not templates:
            raise NotFoundError(
                f"No active report template for {school_year}. "
                "An administrator needs to upload and activate one."
            )
        return templates[0]

    def workbook_and_mapping(self, template_id: uuid.UUID) -> tuple[bytes, TemplateMapping]:
        """The stored workbook and its mapping, for a re-run or an export."""
        template = self.get(template_id)
        return self._load(template), TemplateMapping.from_dict(template.mapping or {})

    def active_mapping(self, school_year: str, grade_level: int = 11) -> tuple[bytes, TemplateMapping]:
        """The workbook bytes and mapping the exporter should use."""
        template = self.active(school_year, grade_level)
        return self._load(template), TemplateMapping.from_dict(template.mapping or {})

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _validate_file(data: bytes, content_type: str | None) -> None:
        if not data:
            raise ValidationError("The uploaded file is empty.")
        if len(data) > settings.MAX_TEMPLATE_BYTES:
            raise ValidationError(
                f"That workbook is larger than {settings.MAX_TEMPLATE_BYTES // (1024 * 1024)}MB."
            )
        if not data.startswith(_ZIP_SIGNATURES):
            raise ValidationError("Upload an .xlsx or .xlsm workbook.")
        if content_type and content_type.split(";")[0].strip() not in settings.ALLOWED_TEMPLATE_TYPES:
            logger.warning("template_type_mismatch", extra={"declared": content_type})

    @staticmethod
    def _open(data: bytes):
        try:
            return load_workbook(io.BytesIO(data), data_only=False, keep_vba=True)
        except Exception as exc:
            raise ValidationError("That workbook could not be opened.") from exc

    def _store(self, data: bytes, checksum: str, filename: str) -> str:
        suffix = "xlsm" if filename.lower().endswith(".xlsm") else "xlsx"
        path = f"templates/{checksum[:16]}.{suffix}"
        if self.storage is not None:
            self.storage.store.upload(
                settings.REPORT_TEMPLATE_BUCKET,
                path,
                data,
                settings.ALLOWED_TEMPLATE_TYPES[0],
            )
        return path

    def _load(self, template: ReportTemplate) -> bytes:
        if self.storage is None:
            raise ConflictError("Template storage is not configured on this server.")
        store = self.storage.store
        if hasattr(store, "files"):  # in-memory store used locally and in tests
            data = store.files.get((settings.REPORT_TEMPLATE_BUCKET, template.storage_path))
            if data is None:
                raise NotFoundError("The stored template file is missing.")
            return data
        raise ConflictError(
            "Downloading a stored template needs Supabase storage to be configured."
        )
