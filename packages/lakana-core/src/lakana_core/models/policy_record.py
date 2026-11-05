"""The columns a row filter and a column mask share."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from lakana_core.models.base import UTCDateTime


class PolicyRecord:
    """The subject, the table binding and the life cycle of a table-scoped policy.

    Mixed into `RowFilter` and `ColumnMask`. Exactly one of `principal_id`/`group_id` is set,
    enforced by a check constraint, and a policy is soft-deleted on revoke
    (`revoked_at`/`revoked_by`).

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier.
    principal_id : uuid.UUID | None
        ID of the principal subject (mutually exclusive with group_id).
    group_id : uuid.UUID | None
        ID of the group subject (mutually exclusive with principal_id).
    environment : str
        The environment where the policy applies.
    namespace : str
        The namespace, stored unit-separator-joined.
    table : str
        The table name the policy applies to.
    environment_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    table_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    created_by : uuid.UUID
        ID of the principal who created the policy.
    created_at : datetime
        Timestamp when the policy was created.
    revoked_by : uuid.UUID | None
        ID of the principal who revoked the policy, if any.
    revoked_at : datetime | None
        Timestamp when the policy was revoked, if any.
    schema_id : int or None
        The id of the schema the type check and the field-id binding of this policy used when
        it was created, or `None` for one created before it was recorded or on a table without
        field ids. For a row with `binding` `pending` or `unbound`, a rename since then may have
        made it cover another column (`lakana_core.policy.renames.renamed_policy_column`); a
        row with `binding` `field_ids` follows its columns by id.
    field_ids : dict of str to int or None
        Each column name the policy uses, as written, to its Iceberg field id (a mask's own
        column included); `None` unless `binding` is `field_ids`.
    binding : str
        `field_ids` (bound at creation or by the worker's backfill), `names` (a policy on a
        raw table, read by name), `pending` (made before the binding, not yet looked at) or
        `unbound` (could not be bound exactly); see `lakana_core.policy.field_binding`.
    """

    @declared_attr.directive
    def __table_args__(cls) -> tuple[CheckConstraint, ...]:
        """Give each table its own check that exactly one subject is set.

        Returns
        -------
        tuple of CheckConstraint
            A new constraint, because a constraint object belongs to one table.
        """
        return (
            CheckConstraint(
                "(principal_id IS NULL) != (group_id IS NULL)", name="exactly_one_subject"
            ),
        )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    environment: Mapped[str] = mapped_column(String(255))
    namespace: Mapped[str] = mapped_column(String(1024))
    table: Mapped[str] = mapped_column(String(255))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tables.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    schema_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_ids: Mapped[dict[str, int] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    binding: Mapped[str] = mapped_column(String(16), server_default="pending")
