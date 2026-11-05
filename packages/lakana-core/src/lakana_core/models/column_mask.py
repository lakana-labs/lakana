"""The column mask model."""

from sqlalchemy import JSON, Boolean, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base
from lakana_core.models.policy_record import PolicyRecord


class ColumnMask(PolicyRecord, Base):
    """One column mask: a subject sees a replacement value instead of one real column.

    Same shape as `RowFilter` — exactly one of `principal_id`/`group_id`,
    always table-scoped, soft-deleted on revoke — plus `column`, the masked
    column's name, and `expression` holding the replacement value rather
    than a predicate.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this column mask.
    principal_id : uuid.UUID | None
        ID of the principal subject (mutually exclusive with group_id).
    group_id : uuid.UUID | None
        ID of the group subject (mutually exclusive with principal_id).
    environment : str
        The environment where this mask applies.
    namespace : str
        The namespace, stored unit-separator-joined.
    table : str
        The table name this mask applies to.
    environment_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    table_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    column : str
        The column name being masked.
    expression : str
        The SQL expression providing the replacement value.
    schema_id : int or None
        The id of the schema the type check and the field-id binding of this column mask used when
        it was created, or `None` for one created before it was recorded or on a table without
        field ids. For a row with `binding` `pending` or `unbound`, a rename since then may have
        made it cover another column (`lakana_core.policy.renames.renamed_policy_column`); a
        row with `binding` `field_ids` follows its columns by id.
    field_ids : dict of str to int or None
        Each column name the expression reads, as written, to its Iceberg field id;
        `None` unless `binding` is `field_ids`.
    column_field_id : int or None
        The field id of the masked column; `None` unless `binding` is `field_ids`.
    binding : str
        `field_ids` (bound at creation or by the worker's backfill), `names` (column mask on a
        raw table, read by name), `pending` (made before the binding, not yet looked at) or
        `unbound` (could not be bound exactly); see `lakana_core.policy.field_binding`.
    type_checked : bool
        Whether the mask was checked against its column's type when it was created
        (`lakana_core.policy.expression_types`). `False` for a mask of an earlier release, which
        a read leaves exactly as written; `True` for a later one, which a read casts to the
        column's type.
    created_by : uuid.UUID
        ID of the principal who created this mask.
    created_at : datetime
        Timestamp when this mask was created.
    revoked_by : uuid.UUID | None
        ID of the principal who revoked this mask, if any.
    revoked_at : datetime | None
        Timestamp when this mask was revoked, if any.
    """

    __tablename__ = "column_masks"

    column: Mapped[str] = mapped_column(String(255))
    expression: Mapped[str] = mapped_column(Text)
    schema_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_ids: Mapped[dict[str, int] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    column_field_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    binding: Mapped[str] = mapped_column(String(16), server_default="pending")
    type_checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
