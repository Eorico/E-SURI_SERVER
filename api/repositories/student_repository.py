"""Students and their section enrolments."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from api.models.enums import EnrollmentStatus
from api.models.tables import Enrollment, Student
from api.repositories.base import SqlRepository


class StudentRepository(SqlRepository[Student]):
    model = Student

    def by_student_no(self, school_id: uuid.UUID, student_no: str) -> Student | None:
        return self.first(school_id=school_id, student_no=student_no)

    def in_section(self, section_id: uuid.UUID) -> list[Student]:
        stmt = (
            select(Student)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .where(Enrollment.section_id == section_id, Enrollment.status == EnrollmentStatus.active)
            .order_by(Student.full_name)
        )
        return list(self.db.scalars(stmt))


class EnrollmentRepository(SqlRepository[Enrollment]):
    model = Enrollment

    def active_in_section(self, section_id: uuid.UUID) -> list[Enrollment]:
        return self.list(section_id=section_id, status=EnrollmentStatus.active)
