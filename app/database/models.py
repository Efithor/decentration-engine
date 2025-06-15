"""database.models
===================

This module contains SQLAlchemy declarative models that back the Postgres
persistence layer.  For now, it is purely illustrative – concrete columns and
relationships will be added once the data-contract is finalized.

Tables to implement
-------------------
1. Cleaning – Tracks chores, their cadence, and completion status.
2. OtherPeople – Stores contact information & relationship context for people
   you interact with (friends, colleagues, contractors, etc.).
3. Exercise – Logs workouts, health metrics, and subjective well-being notes.
4. Projects – Represents active side-projects or work initiatives, including
   milestones and blockers.
5. Media – Catalogs books, articles, videos, and other media you consume or
   wish to consume.

Tips for the future implementation
----------------------------------
* Use `sqlalchemy.dialects.postgresql.JSONB` columns liberally for flexible
  attribute storage (e.g. `metadata` or `tags`).
* Add `created_at` and `updated_at` timestamps via `sqlalchemy.sql.func.now()`.
* Consider alembic for migrations (generate stubs with `alembic revision`).
"""

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    func,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class SCDMixin:
    id = Column(BigInteger, primary_key=True)  # surrogate key
    natural_key = Column(String, nullable=False)  # business key
    measurement_date = Column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    is_current = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # Only one 'current' row per natural_key
        Index(
            "uq_current_record",
            "natural_key",
            unique=True,
            postgresql_where=(is_current.is_(True)),
        ),
    )


class Project(Base, SCDMixin):
    __tablename__ = "projects"
    payload = Column(JSONB)


class Reading(Base, SCDMixin):
    __tablename__ = "reading"
    payload = Column(JSONB)


class Friend(Base, SCDMixin):
    __tablename__ = "friends"
    payload = Column(JSONB)


class Game(Base, SCDMixin):
    __tablename__ = "games"
    payload = Column(JSONB)


# Placeholder imports – uncomment when ready to implement.
# from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
# from sqlalchemy.dialects.postgresql import JSONB
# from sqlalchemy.ext.declarative import declarative_base
#
# Base = declarative_base()
#
# class Cleaning(Base):
#     __tablename__ = "cleaning"
#     id = Column(Integer, primary_key=True)
#     description = Column(String, nullable=False)
#     cadence_days = Column(Integer, default=7)
#     last_completed = Column(DateTime)
#     metadata = Column(JSONB)
#
# class OtherPeople(Base):
#     __tablename__ = "other_people"
#     id = Column(Integer, primary_key=True)
#     name = Column(String, nullable=False)
#     relationship = Column(String)
#     metadata = Column(JSONB)
#
# ...
