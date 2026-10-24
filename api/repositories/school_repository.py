"""Schools, learning areas and sections — the organisational spine."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from api.models.tables import LearningArea, School, Section
from api.repositories.base import SqlRepository


class SchoolRepository(SqlRepository[School]):
    model = School

    def current(self) -> School | None:
        """The deployment serves one school; take the earliest row as canonical."""
        return self.db.scalars(select(School).order_by(School.created_at).limit(1)).first()


class LearningAreaRepository(SqlRepository[LearningArea]):
    model = LearningArea

    def by_code(self, school_id: uuid.UUID, code: str) -> LearningArea | None:
        return self.first(school_id=school_id, code=code)

    def by_sheet_name(self, sheet_name: str) -> LearningArea | None:
        return self.first(sheet_name=sheet_name)

    def ordered(self, school_id: uuid.UUID) -> list[LearningArea]:
        stmt = select(LearningArea).filter_by(school_id=school_id).order_by(LearningArea.name)
        return list(self.db.scalars(stmt))


class SectionRepository(SqlRepository[Section]):
    model = Section

    def ordered(self, school_id: uuid.UUID, school_year: str) -> list[Section]:
        """EQWART column order: `position` first, then name for stability."""
        stmt = (
            select(Section)
            .filter_by(school_id=school_id, school_year=school_year)
            .order_by(Section.position, Section.name)
        )
        return list(self.db.scalars(stmt))
