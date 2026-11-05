"""The row filter model."""

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base
from lakana_core.models.policy_record import PolicyRecord


class RowFilter(PolicyRecord, Base):
    """One row filter: a subject sees only rows matching an expression, on one table.

    Exactly one of `principal_id`/`group_id` is set (the subject) — enforced
    by a check constraint here and, redundantly, by `RowFilterService.create`'s
    own validation, so a violation surfaces as a clean `ValidationError`
    rather than a raw constraint failure.

    Always table-scoped: unlike `Grant`, there is no `kind` column, because
    a row filter's referenced columns have no defined meaning at any wider
    scope. `namespace` is stored unit-separator-joined, matching
    `Grant.namespace`'s convention.

    Soft-deleted on revoke (`revoked_at`/`revoked_by`), matching `Grant`'s
    existing revocation pattern — a revoked filter's row stays, for future
    audit-log value.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row filter.
    principal_id : uuid.UUID | None
        ID of the principal subject (mutually exclusive with group_id).
    group_id : uuid.UUID | None
        ID of the group subject (mutually exclusive with principal_id).
    environment : str
        The environment where this filter applies.
    namespace : str
        The namespace, stored unit-separator-joined.
    table : str
        The table name this filter applies to.
    environment_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    table_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    expression : str
        The SQL expression for filtering rows.
    schema_id : int or None
        The id of the schema the type check and the field-id binding of this row filter used when
        it was created, or `None` for one created before it was recorded or on a table without
        field ids. For a row with `binding` `pending` or `unbound`, a rename since then may have
        made it cover another column (`lakana_core.policy.renames.renamed_policy_column`); a
        row with `binding` `field_ids` follows its columns by id.
    field_ids : dict of str to int or None
        Each column name the expression reads, as written, to its Iceberg field id;
        `None` unless `binding` is `field_ids`.
    binding : str
        `field_ids` (bound at creation or by the worker's backfill), `names` (row filter on a
        raw table, read by name), `pending` (made before the binding, not yet looked at) or
        `unbound` (could not be bound exactly); see `lakana_core.policy.field_binding`.
    created_by : uuid.UUID
        ID of the principal who created this filter.
    created_at : datetime
        Timestamp when this filter was created.
    revoked_by : uuid.UUID | None
        ID of the principal who revoked this filter, if any.
    revoked_at : datetime | None
        Timestamp when this filter was revoked, if any.
    """

    __tablename__ = "row_filters"

    expression: Mapped[str] = mapped_column(Text)
    schema_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_ids: Mapped[dict[str, int] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    binding: Mapped[str] = mapped_column(String(16), server_default="pending")
