"""The audit event model."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.auth import SecurableKind
from lakana_core.models.base import Base, UTCDateTime


class AuditEventType(enum.StrEnum):
    """The kind of event one `AuditEvent` row records."""

    AUTHENTICATION_SUCCEEDED = "AUTHENTICATION_SUCCEEDED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    GRANT_CREATED = "GRANT_CREATED"
    GRANT_REVOKED = "GRANT_REVOKED"
    SECRET_CREATED = "SECRET_CREATED"  # nosec B105 -- an AuditEventType label, not a credential
    SECRET_ROTATED = "SECRET_ROTATED"  # nosec B105 -- an AuditEventType label, not a credential
    SECRET_REVOKED = "SECRET_REVOKED"  # nosec B105 -- an AuditEventType label, not a credential
    SECRET_BOUND = "SECRET_BOUND"  # nosec B105 -- an AuditEventType label, not a credential
    CONNECTION_CREATED = "CONNECTION_CREATED"
    CONNECTION_UPDATED = "CONNECTION_UPDATED"
    CONNECTION_DELETED = "CONNECTION_DELETED"
    CONNECTION_TESTED = "CONNECTION_TESTED"
    CONNECTION_CREDENTIAL_FETCHED = "CONNECTION_CREDENTIAL_FETCHED"
    CONNECTION_CREDENTIAL_REFUSED = "CONNECTION_CREDENTIAL_REFUSED"
    QUERY_COMPLETED = "QUERY_COMPLETED"
    QUERY_CANCELLED = "QUERY_CANCELLED"
    ROW_FILTER_CREATED = "ROW_FILTER_CREATED"
    ROW_FILTER_REVOKED = "ROW_FILTER_REVOKED"
    COLUMN_MASK_CREATED = "COLUMN_MASK_CREATED"
    COLUMN_MASK_REVOKED = "COLUMN_MASK_REVOKED"
    GROUP_CREATED = "GROUP_CREATED"
    GROUP_DELETED = "GROUP_DELETED"
    GROUP_MEMBER_ADDED = "GROUP_MEMBER_ADDED"
    GROUP_MEMBER_REMOVED = "GROUP_MEMBER_REMOVED"
    PAT_ISSUED = "PAT_ISSUED"
    PAT_REVOKED = "PAT_REVOKED"
    TASK_TOKEN_MINTED = "TASK_TOKEN_MINTED"  # nosec B105 -- an AuditEventType label, not a credential
    TASK_TOKEN_REVOKED = "TASK_TOKEN_REVOKED"  # nosec B105 -- an AuditEventType label, not a credential
    PRINCIPAL_CREATED = "PRINCIPAL_CREATED"
    PRINCIPAL_DEACTIVATED = "PRINCIPAL_DEACTIVATED"
    ENVIRONMENT_CREATED = "ENVIRONMENT_CREATED"
    ENVIRONMENT_DELETED = "ENVIRONMENT_DELETED"
    ENVIRONMENT_STORAGE_UPDATED = "ENVIRONMENT_STORAGE_UPDATED"
    ENVIRONMENT_PROTECTION_CHANGED = "ENVIRONMENT_PROTECTION_CHANGED"
    BOOTSTRAP_ADMIN_RESTORED = "BOOTSTRAP_ADMIN_RESTORED"
    TABLE_APPENDED = "TABLE_APPENDED"
    TABLE_WRITE = "TABLE_WRITE"
    TABLE_SCHEMA_CHANGED = "TABLE_SCHEMA_CHANGED"
    TABLE_MAINTAINED = "TABLE_MAINTAINED"
    REPOSITORY_CREATED = "REPOSITORY_CREATED"
    REPOSITORY_UPDATED = "REPOSITORY_UPDATED"
    REPOSITORY_DELETED = "REPOSITORY_DELETED"
    PACKAGE_INDEX_CREATED = "PACKAGE_INDEX_CREATED"
    PACKAGE_INDEX_UPDATED = "PACKAGE_INDEX_UPDATED"
    PACKAGE_INDEX_DELETED = "PACKAGE_INDEX_DELETED"
    DEPLOYED = "DEPLOYED"
    DEPLOY_DRY_RUN = "DEPLOY_DRY_RUN"
    ROLLED_BACK = "ROLLED_BACK"
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_CANCEL_REQUESTED = "RUN_CANCEL_REQUESTED"
    CODE_INTEGRITY_FAILED = "CODE_INTEGRITY_FAILED"
    PROTECTED_WRITE_DENIED = "PROTECTED_WRITE_DENIED"
    BREAK_GLASS_WRITE = "BREAK_GLASS_WRITE"
    RUN_AS_BOUND = "RUN_AS_BOUND"
    RUN_AS_CLEARED = "RUN_AS_CLEARED"
    UNDEPLOYED = "UNDEPLOYED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    ARTIFACT_VERSION_ADDED = "ARTIFACT_VERSION_ADDED"
    ARTIFACT_DOWNLOADED = "ARTIFACT_DOWNLOADED"
    ARTIFACT_VERSION_DELETED = "ARTIFACT_VERSION_DELETED"
    ARTIFACT_DROPPED = "ARTIFACT_DROPPED"
    CHECK_FAILED = "CHECK_FAILED"
    TABLE_ROLLED_BACK = "TABLE_ROLLED_BACK"
    TABLE_KEPT = "TABLE_KEPT"
    TABLE_KEPT_CLEARED = "TABLE_KEPT_CLEARED"
    JOB_PAUSED = "JOB_PAUSED"
    JOB_RESUMED = "JOB_RESUMED"
    JOB_START_WAITING = "JOB_START_WAITING"
    JOB_START_DROPPED = "JOB_START_DROPPED"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
    NOTIFICATION_TESTED = "NOTIFICATION_TESTED"
    TABLE_DESCRIBED = "TABLE_DESCRIBED"
    COLUMN_DESCRIBED = "COLUMN_DESCRIBED"
    TABLE_LOCATION_ABANDONED = "TABLE_LOCATION_ABANDONED"
    TABLE_PURGED = "TABLE_PURGED"
    TABLE_DROPPED = "TABLE_DROPPED"
    TABLE_UNDROPPED = "TABLE_UNDROPPED"
    TABLE_RETENTION_SET = "TABLE_RETENTION_SET"
    TABLE_RETENTION_CLEARED = "TABLE_RETENTION_CLEARED"
    ENVIRONMENT_RETENTION_SET = "ENVIRONMENT_RETENTION_SET"
    ENVIRONMENT_RESTORED = "ENVIRONMENT_RESTORED"
    ENVIRONMENT_PURGED = "ENVIRONMENT_PURGED"
    ORPHANED_LOCATIONS_DELETED = "ORPHANED_LOCATIONS_DELETED"
    ACTIVITY_VIEWED = "ACTIVITY_VIEWED"
    CI_TRUST_RULE_CREATED = "CI_TRUST_RULE_CREATED"
    CI_TRUST_RULE_UPDATED = "CI_TRUST_RULE_UPDATED"
    CI_TRUST_RULE_DELETED = "CI_TRUST_RULE_DELETED"
    CI_TRUST_RULE_ENABLED = "CI_TRUST_RULE_ENABLED"
    CI_TRUST_RULE_DISABLED = "CI_TRUST_RULE_DISABLED"
    OIDC_TOKEN_EXCHANGED = "OIDC_TOKEN_EXCHANGED"  # nosec B105 -- an event name, not a credential
    OIDC_EXCHANGE_REFUSED = "OIDC_EXCHANGE_REFUSED"


class AuditEvent(Base):
    """One append-only record of a security-relevant event.

    Never updated or deleted after creation — there is no `revoked_at`-style
    field, unlike every other M3 model, because there is nothing to
    soft-delete an audit event *into*. `AuditService` (the only code that
    ever writes this table) exposes no update or delete method at all; that
    omission is the whole enforcement mechanism, the same way "the REST API
    is the only way in" is enforced elsewhere in this codebase.

    `principal_id` is nullable with `ON DELETE SET NULL`, matching
    `QueryRun.principal_id`'s precedent exactly: an audit trail outlives its
    author, and a failed authentication has no verified principal to
    reference in the first place.

    `securable_kind`/`securable_environment`/`securable_namespace`/
    `securable_table` mirror `lakana_core.auth.Securable`'s own shape and are
    populated whenever an event has a natural securable target — every
    event type this plan covers except authentication, which precedes
    authorization entirely and has none. `securable_namespace` is stored
    unit-separator-joined, matching `Grant.namespace`'s convention.

    `detail` carries whatever facts are specific to `event_type` and not
    already captured by the columns above (a secret's name, a grant's
    subject, a query's SQL text, the specific `Action` an
    `AUTHORIZATION_DENIED` event refused) — structurally bounded per type
    by whichever service constructs the event, never a superset "just in
    case". Defaults to `{}` only for an event type with genuinely no extra
    facts to carry beyond its columns.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # the window lookup of `lakana_core.audit.views.record_view`
        Index(
            "ix_audit_events_event_type_principal_id_occurred_at",
            "event_type",
            "principal_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[AuditEventType] = mapped_column(
        SAEnum(AuditEventType, native_enum=False, length=64), index=True
    )
    securable_kind: Mapped[SecurableKind | None] = mapped_column(
        SAEnum(SecurableKind, native_enum=False), nullable=True
    )
    securable_environment: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    securable_namespace: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    securable_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
