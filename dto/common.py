"""Shared DTO building blocks.

DTOs are the only shapes that cross the HTTP boundary. Documents never do, so
a column added to a table cannot accidentally become public.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class DTO(BaseModel):
    """Request DTOs: reject unknown fields rather than ignore them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMDto(BaseModel):
    """Response DTOs: built straight from document instances.

    `from_attributes` is what lets a route hand a document straight to a DTO,
    including its computed properties - `full_name` is assembled rather than
    stored, and this is how it reaches the client.
    """

    model_config = ConfigDict(from_attributes=True)


class Page(ORMDto, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageParams(DTO):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class Message(ORMDto):
    message: str


class IdRef(ORMDto):
    id: uuid.UUID


__all__ = ["DTO", "ORMDto", "Page", "PageParams", "Message", "IdRef", "date", "datetime", "uuid"]
