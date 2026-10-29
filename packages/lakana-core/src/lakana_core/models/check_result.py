"""The result of one data quality check of one task of a run."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class CheckOutcome(enum.StrEnum):
    """How one check ended."""

    PASSED = "passed"
    FAILED = "failed"
    WARNED = "warned"
    ERRORED = "errored"


class RollbackOutcome(enum.StrEnum):
    """What the rollback after a failed blocking check did to the checked table."""

    ROLLED_BACK = "rolled_back"
    UNPUBLISHED = "unpublished"
    NOTHING_WRITTEN = "nothing_written"
    NOT_A_TABLE = "not_a_table"
    CHANGED_SINCE = "changed_since"
    FAILED = "failed"


class CheckResult(Base):
    """One check's result for one task run.

    A result holds a count and, for an error, the error text; never a row of a table.
    `table_id` has no foreign key: a table that was dropped keeps its results.

    Attributes
    ----------
    generated : bool
        Whether the check was generated from the asset's header (`key:`, `references:`)
        rather than written under `checks/`; false on every result recorded before it existed.
    """

    __tablename__ = "check_results"
    __table_args__ = (
        UniqueConstraint("task_run_id", "check_name"),
        Index("ix_check_results_environment_id_finished_at", "environment_id", "finished_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    check_name: Mapped[str] = mapped_column(String(1024))
    check_path: Mapped[str] = mapped_column(String(1024))
    table_name: Mapped[str] = mapped_column(String(1024))
    table_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    on_failure: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[CheckOutcome] = mapped_column(
        SAEnum(CheckOutcome, native_enum=False, length=16)
    )
    failing_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rollback_outcome: Mapped[RollbackOutcome | None] = mapped_column(
        SAEnum(RollbackOutcome, native_enum=False, length=16), nullable=True
    )
    policy_applied: Mapped[bool] = mapped_column(Boolean, server_default=false())
    generated: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("query_runs.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
