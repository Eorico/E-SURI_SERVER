"""Students and their section enrolments."""

from __future__ import annotations

import uuid

from pymongo import ASCENDING

from api.models.documents import Enrollment, Student
from api.models.enums import EnrollmentStatus
from api.repositories.base import MongoRepository, as_uuid


class StudentRepository(MongoRepository[Student]):
    model = Student
    default_sort = (("full_name", ASCENDING),)

    def by_student_no(self, school_id: uuid.UUID, student_no: str) -> Student | None:
        return self.first(school_id=school_id, student_no=student_no)

    def existing_ids(self, student_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these ids are real students, in one query.

        Used before a bulk enrolment. Checking them one at a time meant a
        section of forty learners cost forty round trips, and only to discard
        ids that do not exist.
        """
        if not student_ids:
            return set()
        found = self.collection.find({"_id": {"$in": student_ids}}, projection={"_id": 1})
        # UUIDs, because `ClassService.enrol` tests `student_id in real` with
        # the ids it was handed. A set of strings would discard every learner.
        return {as_uuid(document["_id"]) for document in found}

    def by_ids(self, student_ids: list[uuid.UUID]) -> list[Student]:
        """Several learners in one query, in name order."""
        if not student_ids:
            return []
        cursor = self.collection.find({"_id": {"$in": student_ids}}).sort(
            list(self.default_sort)
        )
        return self._many(cursor)

    def in_section(self, section_id: uuid.UUID) -> list[Student]:
        """The learners actively enrolled in a section.

        Two indexed queries where the SQL was one join. Resolving the ids first
        means each step uses its own index - `ix_enrollments_section`, then the
        primary key - rather than asking the server to join and then sort.
        """
        enrolled = self.other("enrollments").find(
            {"section_id": section_id, "status": EnrollmentStatus.active.value},
            projection={"student_id": 1},
        )
        return self.by_ids([as_uuid(document["student_id"]) for document in enrolled])


class EnrollmentRepository(MongoRepository[Enrollment]):
    model = Enrollment

    def active_in_section(self, section_id: uuid.UUID) -> list[Enrollment]:
        return self.list(section_id=section_id, status=EnrollmentStatus.active)
