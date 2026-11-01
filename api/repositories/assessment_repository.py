"""Assessments, answer keys and generated answer sheets."""

from __future__ import annotations

import uuid
from typing import Sequence

from pymongo import ASCENDING

from api.models.documents import AnswerKey, AnswerSheet, Assessment
from api.models.enums import AssessmentStatus, AssessmentType
from api.repositories.base import EmbeddedRepository, MongoRepository
from api.repositories.class_repository import ClassRepository


class AssessmentRepository(MongoRepository[Assessment]):
    model = Assessment
    default_sort = (("date", ASCENDING), ("created_at", ASCENDING))

    def by_class(self, class_id: uuid.UUID) -> list[Assessment]:
        return self.list(class_id=class_id)

    def by_code(self, test_code: str) -> Assessment | None:
        return self.first(test_code=test_code)

    def get_full(self, assessment_id: uuid.UUID) -> Assessment | None:
        """An assessment with its class, and the class with its section.

        The exporter reads `assessment.class_.section`, so both levels are
        filled - the class, and the class's own slots with it.
        """
        record = self.get(assessment_id)
        return self._hydrate([record])[0] if record else None

    def for_learning_area(
        self, learning_area_id: uuid.UUID, assessment_type: AssessmentType, school_year: str
    ) -> list[Assessment]:
        """Every section's copy of one assessment — the columns of an EQWART sheet.

        The classes are resolved first, then their assessments by id. Two
        indexed queries beat one aggregation here: `$lookup` would join every
        assessment of that type to its class and filter afterwards, while this
        narrows on `ix_classes_area` before touching assessments at all.
        """
        classes = ClassRepository(self.db).by_learning_area(learning_area_id, school_year)
        if not classes:
            return []

        by_class = {record.id: record for record in classes}
        cursor = self.collection.find(
            {"class_id": {"$in": list(by_class)}, "type": assessment_type.value}
        ).sort(list(self.default_sort))

        records = self._many(cursor)
        for record in records:
            record.class_ = by_class.get(record.class_id)
        return records

    def for_section(self, section_id: uuid.UUID, school_year: str) -> list[Assessment]:
        class_ids = [
            document["_id"]
            for document in self.other("classes").find(
                {"section_id": section_id, "school_year": school_year},
                projection={"_id": 1},
            )
        ]
        if not class_ids:
            return []
        cursor = self.collection.find({"class_id": {"$in": class_ids}}).sort(
            list(self.default_sort)
        )
        return self._many(cursor)

    def mark_in_progress(self, assessment: Assessment) -> Assessment:
        if assessment.status in (AssessmentStatus.draft, AssessmentStatus.scheduled):
            return self.update(assessment, status=AssessmentStatus.in_progress)
        return assessment

    def _hydrate(self, records: Sequence[Assessment]) -> list[Assessment]:
        """Fill `class_`, and the class's own slots with it.

        The classes come back in one query and are hydrated as a batch, so this
        costs the same four reads whether it is called with one assessment or
        forty.
        """
        present = [record for record in records if record is not None]
        if not present:
            return []

        classes = ClassRepository(self.db)
        wanted = {record.class_id for record in present if record.class_id is not None}
        found = classes.collection.find({"_id": {"$in": list(wanted)}}) if wanted else []
        hydrated = classes.hydrate(classes._many(found))

        by_id = {record.id: record for record in hydrated}
        for record in present:
            record.class_ = by_id.get(record.class_id)
        return list(present)


class AnswerKeyRepository(EmbeddedRepository[AnswerKey]):
    """The correct option per item, inside its assessment.

    Embedded: a key is never read without the assessment it belongs to, and is
    replaced wholesale when a teacher edits it. As a table it was up to sixty
    rows per assessment and a join on every single scan.
    """

    model = AnswerKey
    parent_model = Assessment
    array_field = "answer_keys"
    parent_key = "assessment_id"
    order_by = "item_no"

    def for_assessment(self, assessment_id: uuid.UUID) -> list[AnswerKey]:
        return self.for_parent(assessment_id)

    def as_map(self, assessment_id: uuid.UUID) -> dict[int, str]:
        """Maps item number to correct option — what the scorer needs."""
        return {key.item_no: key.correct_option for key in self.for_assessment(assessment_id)}

    def item(self, assessment_id: uuid.UUID, item_no: int) -> AnswerKey | None:
        return self.first(assessment_id=assessment_id, item_no=item_no)


class AnswerSheetRepository(MongoRepository[AnswerSheet]):
    model = AnswerSheet

    def for_assessment(self, assessment_id: uuid.UUID) -> list[AnswerSheet]:
        return self.list(assessment_id=assessment_id)

    def for_student(self, assessment_id: uuid.UUID, student_id: uuid.UUID) -> AnswerSheet | None:
        return self.first(assessment_id=assessment_id, student_id=student_id)
