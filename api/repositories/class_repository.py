"""Classes — one learning area taught to one section for a school year."""

from __future__ import annotations

import uuid
from typing import Iterable, Sequence, TypeVar

from pymongo import ASCENDING

from api.models.documents import Class, LearningArea, Profile, Section, Student
from api.models.enums import EnrollmentStatus
from api.core.cache import cached
from api.repositories.base import MongoRepository, as_uuid

T = TypeVar("T")


class ClassRepository(MongoRepository[Class]):
    model = Class
    default_sort = (("school_year", ASCENDING), ("class_code", ASCENDING))

    # --- reads ------------------------------------------------------------
    def get_full(self, class_id: uuid.UUID) -> Class | None:
        """A class with its section, learning area and teacher filled in."""
        record = self.get(class_id)
        return self.hydrate([record])[0] if record else None

    @cached()
    def by_teacher(self, teacher_id: uuid.UUID) -> list[Class]:
        """A teacher's own classes - the first thing their dashboard reads.

        Cached because it is four round trips (the classes, then their
        sections, subjects and teachers) and changes about once a term. A write
        to `classes` clears it, and so does a write to `sections`,
        `learning_areas` or `profiles`, because the hydrated result holds a
        copy of each - see `depends_on` on those repositories.
        """
        return self.hydrate(self.list(teacher_id=teacher_id))

    @cached()
    def by_learning_area(self, learning_area_id: uuid.UUID, school_year: str) -> list[Class]:
        """Every section taking one subject - a coordinator's whole screen, and
        the column set of an EQWART sheet."""
        return self.hydrate(
            self.list(learning_area_id=learning_area_id, school_year=school_year)
        )

    def by_term(self, term_id: uuid.UUID) -> list[Class]:
        """Every class in one assessment cycle."""
        return self.hydrate(self.list(term_id=term_id))

    def all_full(self) -> list[Class]:
        return self.hydrate(self.list())

    def by_code(self, class_code: str) -> Class | None:
        return self.first(class_code=class_code)

    def roster(self, class_id: uuid.UUID) -> list[Student]:
        """Learners enrolled in this class's section, in name order.

        Three indexed reads where the SQL was one three-table join: the class,
        then its section's active enrolments, then those learners. Each step
        hits an index of its own, and the last is a primary-key fetch.
        """
        record = self.get(class_id)
        if record is None:
            return []

        enrolled = self.other("enrollments").find(
            {"section_id": record.section_id, "status": EnrollmentStatus.active.value},
            projection={"student_id": 1},
        )
        student_ids = [as_uuid(document["student_id"]) for document in enrolled]
        if not student_ids:
            return []

        found = self.other("students").find({"_id": {"$in": student_ids}}).sort(
            [("full_name", ASCENDING)]
        )
        return [Student.model_validate(document) for document in found]

    # --- hydration --------------------------------------------------------
    def hydrate(self, records: Sequence[Class]) -> list[Class]:
        """Fill the section, learning area and teacher slots on every class.

        Public because `AssessmentRepository` hydrates the classes behind its
        own assessments with it, rather than repeating the three queries.

        Three queries no matter how many classes, which is the point.
        Resolving each class's relations one at a time would be three per
        class - and on a coordinator's screen that is one class per section,
        so forty classes would mean a hundred and twenty round trips.
        """
        records = [record for record in records if record is not None]
        if not records:
            return []

        sections = self._by_id(
            "sections", {record.section_id for record in records}, Section
        )
        areas = self._by_id(
            "learning_areas", {record.learning_area_id for record in records}, LearningArea
        )
        teachers = self._by_id(
            "users", {record.teacher_id for record in records}, Profile
        )

        for record in records:
            record.section = sections.get(record.section_id)
            record.learning_area = areas.get(record.learning_area_id)
            record.teacher = teachers.get(record.teacher_id)
        return list(records)

    def _by_id(
        self, collection: str, ids: Iterable[uuid.UUID], model: type[T]
    ) -> dict[uuid.UUID, T]:
        """One primary-key query, returned as a lookup table.

        A missing id yields a missing key rather than an error: what used to be
        `ON DELETE SET NULL` leaves ids pointing at nothing, and a class whose
        section was deleted must still be listable.
        """
        wanted = [identifier for identifier in ids if identifier is not None]
        if not wanted:
            return {}
        found = self.other(collection).find({"_id": {"$in": wanted}})
        # Keyed on the validated model's id, which is a `uuid.UUID`. Keying on
        # the raw `document["_id"]` makes this a map of strings that no caller
        # can look anything up in, because every caller holds a UUID.
        loaded = [model.model_validate(document) for document in found]
        return {record.id: record for record in loaded}
