"""Pydantic request/response schemas.

ORMModel is the shared base (from_attributes) so every output schema reuses the
same config instead of repeating it (DRY).
"""
import uuid
from datetime import date as _date, datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Profiles ---------------------------------------------------------------
class ProfileOut(ORMModel):
    id: uuid.UUID
    role: str
    full_name: str | None = None
    username: str | None = None
    school_id: uuid.UUID | None = None


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    username: str | None = None


# --- Classes ----------------------------------------------------------------
class ClassCreate(BaseModel):
    grade_level: int
    section_name: str
    subject: str
    adviser: str | None = None
    school_id: uuid.UUID | None = None


class ClassOut(ORMModel):
    id: uuid.UUID
    grade_level: int
    section_name: str
    subject: str
    adviser: str | None = None
    class_code: str
    teacher_id: uuid.UUID
    school_id: uuid.UUID | None = None
    created_at: datetime


# --- Assessments ------------------------------------------------------------
class AssessmentCreate(BaseModel):
    title: str
    type: str  # pre | st1 | st2 | term | post
    class_id: uuid.UUID
    subject: str | None = None
    date: _date | None = None


class AssessmentOut(ORMModel):
    id: uuid.UUID
    title: str
    type: str
    class_id: uuid.UUID
    subject: str | None = None
    item_count: int
    test_code: str
    date: _date | None = None
    status: str
    created_at: datetime
