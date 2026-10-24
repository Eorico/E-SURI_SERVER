"""Classes, sections and rosters.

A class is one learning area taught to one section for a school year, which is
exactly how the EQWART workbook is organised. Visibility rules live here so no
controller has to reason about who may see what (SRP).
"""

from __future__ import annotations

import secrets
import uuid

from api.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from api.core.security import Principal
from api.models.enums import EnrollmentStatus, UserRole
from api.models.tables import Class, Enrollment, Section, Student
from api.repositories.class_repository import ClassRepository
from api.repositories.school_repository import LearningAreaRepository, SectionRepository
from api.repositories.student_repository import EnrollmentRepository, StudentRepository


class ClassService:
    def __init__(
        self,
        classes: ClassRepository,
        sections: SectionRepository,
        learning_areas: LearningAreaRepository,
        students: StudentRepository,
        enrollments: EnrollmentRepository,
    ):
        self.classes = classes
        self.sections = sections
        self.learning_areas = learning_areas
        self.students = students
        self.enrollments = enrollments

    # --- reads ------------------------------------------------------------
    def visible_to(self, principal: Principal) -> list[Class]:
        """Teachers see their own classes; coordinators their learning area;
        administrators everything."""
        if principal.role is UserRole.administrator:
            return self.classes.all_full()
        if principal.role is UserRole.subject_coordinator and principal.learning_area_id:
            return self.classes.by_learning_area(
                principal.learning_area_id, self._school_year(principal)
            )
        return self.classes.by_teacher(principal.id)

    def get_for(self, principal: Principal, class_id: uuid.UUID) -> Class:
        record = self.classes.get_full(class_id)
        if record is None:
            raise NotFoundError("That class does not exist.")
        self.assert_can_view(principal, record)
        return record

    def assert_can_view(self, principal: Principal, record: Class) -> None:
        if principal.role is UserRole.administrator:
            return
        if record.teacher_id == principal.id:
            return
        if (
            principal.role is UserRole.subject_coordinator
            and principal.learning_area_id == record.learning_area_id
        ):
            return
        raise PermissionDeniedError("This class belongs to another teacher.")

    def assert_can_edit(self, principal: Principal, record: Class) -> None:
        """Only the owning teacher — or an administrator — may change a class."""
        if principal.role is UserRole.administrator or record.teacher_id == principal.id:
            return
        raise PermissionDeniedError("Only the subject teacher can change this class.")

    def roster(self, principal: Principal, class_id: uuid.UUID) -> list[Student]:
        self.get_for(principal, class_id)
        return self.classes.roster(class_id)

    # --- writes -----------------------------------------------------------
    def create(
        self,
        principal: Principal,
        *,
        section_id: uuid.UUID,
        learning_area_id: uuid.UUID,
        school_year: str,
    ) -> Class:
        section = self.sections.get(section_id)
        if section is None:
            raise NotFoundError("That section does not exist.")
        if self.learning_areas.get(learning_area_id) is None:
            raise NotFoundError("That learning area does not exist.")

        if self.classes.exists(
            section_id=section_id, learning_area_id=learning_area_id, school_year=school_year
        ):
            raise ConflictError("That section already has a class for this learning area.")

        record = Class(
            section_id=section_id,
            learning_area_id=learning_area_id,
            teacher_id=principal.id,
            class_code=self._unique_code(section),
            school_year=school_year,
        )
        return self.classes.add(record)

    def enroll(
        self, principal: Principal, class_id: uuid.UUID, student_ids: list[uuid.UUID]
    ) -> list[Enrollment]:
        """Enrol learners into the class's section, skipping anyone already there."""
        record = self.classes.get_full(class_id)
        if record is None:
            raise NotFoundError("That class does not exist.")
        self.assert_can_edit(principal, record)

        existing = {
            enrollment.student_id
            for enrollment in self.enrollments.active_in_section(record.section_id)
        }
        fresh = [
            Enrollment(
                student_id=student_id,
                section_id=record.section_id,
                status=EnrollmentStatus.active,
            )
            for student_id in student_ids
            if student_id not in existing and self.students.get(student_id) is not None
        ]
        return self.enrollments.add_all(fresh) if fresh else []

    # --- helpers ----------------------------------------------------------
    def _unique_code(self, section: Section) -> str:
        slug = "".join(ch for ch in section.name.upper() if ch.isalnum())[:8] or "CLASS"
        for _ in range(8):
            code = f"{slug}-{secrets.token_hex(2).upper()}"
            if self.classes.by_code(code) is None:
                return code
        raise ConflictError("Could not allocate a unique class code. Try again.")

    @staticmethod
    def _school_year(principal: Principal) -> str:
        school = principal.profile.school_id
        # The school year lives on the school row; callers that need a different
        # year pass it explicitly. Falling back keeps coordinators working when
        # the profile has no school attached yet.
        return getattr(principal.profile, "school_year", None) or _current_school_year(school)


def _current_school_year(_school_id: uuid.UUID | None) -> str:
    """Placeholder until a school is attached; overridden by the school record."""
    from datetime import date

    today = date.today()
    start = today.year if today.month >= 6 else today.year - 1
    return f"{start}-{start + 1}"
