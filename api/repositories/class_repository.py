"""Classes — one learning area taught to one section for a school year."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from api.models.tables import Class, Enrollment, Student
from api.models.enums import EnrollmentStatus
from api.repositories.base import SqlRepository


class ClassRepository(SqlRepository[Class]):
    model = Class

    def _with_relations(self):
        return select(Class).options(
            joinedload(Class.section),
            joinedload(Class.learning_area),
            joinedload(Class.teacher),
        )

    def get_full(self, class_id: uuid.UUID) -> Class | None:
        return self.db.scalars(self._with_relations().filter(Class.id == class_id)).first()

    def by_teacher(self, teacher_id: uuid.UUID) -> list[Class]:
        return list(self.db.scalars(self._with_relations().filter(Class.teacher_id == teacher_id)))

    def by_learning_area(self, learning_area_id: uuid.UUID, school_year: str) -> list[Class]:
        stmt = self._with_relations().filter(
            Class.learning_area_id == learning_area_id, Class.school_year == school_year
        )
        return list(self.db.scalars(stmt))

    def all_full(self) -> list[Class]:
        return list(self.db.scalars(self._with_relations()))

    def by_code(self, class_code: str) -> Class | None:
        return self.first(class_code=class_code)

    def roster(self, class_id: uuid.UUID) -> list[Student]:
        """Learners enrolled in this class's section, in name order."""
        stmt = (
            select(Student)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .join(Class, Class.section_id == Enrollment.section_id)
            .where(Class.id == class_id, Enrollment.status == EnrollmentStatus.active)
            .order_by(Student.full_name)
        )
        return list(self.db.scalars(stmt))
