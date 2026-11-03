"""Schools, learning areas and sections — the organisational spine."""

from __future__ import annotations

import uuid

from pymongo import ASCENDING

from api.core.cache import cached
from api.models.documents import Department, LearningArea, School, Section
from api.repositories.base import MongoRepository


class SchoolRepository(MongoRepository[School]):
    model = School

    @cached()
    def current(self) -> School | None:
        """The deployment serves one school; take the earliest document as canonical.

        Read on nearly every write path to resolve `school_id`, and it changes
        about once a year.
        """
        return self.first()


class DepartmentRepository(MongoRepository[Department]):
    """The five groupings a subject reports under.

    A master teacher is scoped to exactly one, so this is read on the sign-up
    form as well as everywhere their reports are filtered.
    """

    model = Department
    default_sort = (("name", ASCENDING),)
    #: An administrator's embedded details carry a department id, and a
    #: hydrated class reaches a department through its subject.
    depends_on = ("users", "learning_areas")

    @cached()
    def ordered(self, school_id: uuid.UUID) -> list[Department]:
        return self.list(school_id=school_id)

    def by_code(self, school_id: uuid.UUID, code: str) -> Department | None:
        return self.first(school_id=school_id, code=code.upper())


class LearningAreaRepository(MongoRepository[LearningArea]):
    model = LearningArea
    default_sort = (("name", ASCENDING),)
    #: Hydrated classes and assessments embed a copy of the subject.
    depends_on = ("classes", "assessments")

    def by_code(self, school_id: uuid.UUID, code: str) -> LearningArea | None:
        return self.first(school_id=school_id, code=code)

    def by_sheet_name(self, sheet_name: str) -> LearningArea | None:
        return self.first(sheet_name=sheet_name)

    @cached()
    def ordered(self, school_id: uuid.UUID) -> list[LearningArea]:
        """The subject list, which every report and picker reads."""
        return self.list(school_id=school_id)

    def in_department(self, department_id: uuid.UUID) -> list[LearningArea]:
        """The subjects a master teacher oversees — what scopes their reports."""
        return self.list(department_id=department_id)


class SectionRepository(MongoRepository[Section]):
    model = Section
    #: A hydrated class carries a copy of its section, so renaming one has to
    #: clear the classes as well as the sections.
    depends_on = ("classes",)
    #: EQWART column order: `position` first, then name for stability.
    default_sort = (("position", ASCENDING), ("name", ASCENDING))

    @cached()
    def ordered(self, school_id: uuid.UUID, school_year: str) -> list[Section]:
        """The EQWART column order - read once per sheet, per export."""
        return self.list(school_id=school_id, school_year=school_year)
