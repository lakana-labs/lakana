"""Domain errors raised by lakana-core.

These carry no HTTP knowledge; the server layer is responsible for mapping
them onto status codes and response bodies.
"""

from typing import Literal, NamedTuple


class LakanaError(Exception):
    """Base class for every error raised by lakana-core."""


class NotFoundError(LakanaError):
    """A requested entity does not exist."""


class AlreadyExistsError(LakanaError):
    """An entity with the same identity already exists."""


class ConflictError(LakanaError):
    """A concurrent change was detected, or a stated requirement was not met."""


class ValidationError(LakanaError):
    """Input was structurally invalid."""


class SqlPosition(NamedTuple):
    """A place in submitted SQL, counted from 1 as an editor counts.

    Parameters
    ----------
    line
        The line, from 1.
    column
        The column in that line, from 1.
    """

    line: int
    """The line, from 1."""
    column: int
    """The column in that line, from 1."""


class SqlRefusal(ValidationError):
    """The query guard refused the SQL, and may know where in it the cause is.

    A `ValidationError`, so every handler of one (the 422 mapping among them) treats it the
    same. The message is unchanged by the position.

    Parameters
    ----------
    message
        What is refused and what to do next.
    position
        The place in the submitted SQL of the refused node, or `None` when it has none.
    """

    def __init__(self, message: str, position: SqlPosition | None = None) -> None:
        super().__init__(message)
        self.position = position
        """The place in the submitted SQL, or `None`."""


class UnsupportedPicture(ValidationError):
    """A profile picture was sent as a type Lakana does not read (only JPEG, PNG and WebP).

    Raised by `lakana_core.identity.pictures.reencode_picture` for any other media type,
    SVG among them, before a byte is decoded. Mapped to 415 by the server.
    """


class NoWorkerError(LakanaError):
    """No worker is running that could take the job, so queueing it would only make it wait.

    Raised by `lakana_core.projects.probes.RepositoryProbeService` before a repository check is
    queued. Mapped to 503 by the server.
    """


class PermissionDenied(LakanaError):
    """The principal is not permitted to perform this action."""


class AuthenticationError(LakanaError):
    """The request's credentials are missing or malformed.

    Distinct from `PermissionDenied`: this means the caller could not be
    identified at all (maps to HTTP 401), where `PermissionDenied` means a
    real, identified principal lacks a right (maps to 403). M1 has no real
    credential verification, so in practice this fires only when the
    `Authorization` header is absent or does not match the expected shape —
    the secret itself is never checked. M3 is expected to raise this for a
    revoked or expired PAT too, which is why the distinction exists now
    rather than being introduced later as a breaking change to the mapping.
    """


_SESSION_ENDED = {
    "idle": "Your session ended after a time without activity. Sign in again.",
    "lifetime": "Your session reached its maximum length and ended. Sign in again.",
    "sign_out": "You signed out. Sign in again.",
    "sign_out_everywhere": "You signed out on every device. Sign in again.",
    "ended_by_admin": "An administrator ended your session. Sign in again.",
    "replaced": "A newer sign-in in this browser replaced this session. Sign in again.",
    "deactivated": (
        "Your Lakana account is deactivated, so your session ended and you can no longer sign "
        "in. If this is a mistake, ask an administrator for a new account."
    ),
    "token_window": (  # nosec B105 -- a reason label and its message, not a credential
        "This session was started with an API token of an account that signs in through your "
        "organisation's identity provider, and nobody has signed in there as you within the "
        "token window, so the session ended. Sign in through the identity provider."
    ),
    "token_revoked": (  # nosec B105 -- a reason label and its message, not a credential
        "The token this session was created with was revoked or expired, so the session ended. "
        "Sign in again."
    ),
    "recovery": (
        "Your account was recovered with a recovery link, which ended every earlier session. "
        "Sign in again with your new passkey."
    ),
    "passkey_copied": (
        "A copy of one of your passkeys was detected; it is disabled and every session of your "
        "account was ended. Sign in with another passkey, or ask an administrator for a recovery "
        "link."
    ),
    "passkey_removed": (
        "An administrator removed one of your passkeys, which ended your sessions. Sign in "
        "again with another passkey, or ask an administrator for a recovery link."
    ),
    "reactivated": (
        "Your account was reactivated, which ended every earlier session and removed its "
        "passkeys. Ask the server operator for a new sign-in link."
    ),
}
"""The message for each reason a session ends; an unknown reason gets a general one."""


class SessionEndedError(AuthenticationError):
    """A browser session was valid once and has ended.

    Distinct from a plain `AuthenticationError` (an unknown or forged cookie) so the interface
    can say why: the server answers 401 with `reason`.

    Parameters
    ----------
    reason
        Why it ended: ``idle``, ``lifetime``, ``sign_out``, ``sign_out_everywhere``,
        ``ended_by_admin``, ``replaced``, ``deactivated``, ``token_revoked``, or another
        reason a caller of `SessionService.end_for_principal` gave.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            _SESSION_ENDED.get(reason, f"Your session ended ({reason}). Sign in again.")
        )


class StepUpRequired(PermissionDenied):
    """The action needs a fresh passkey touch in this browser session.

    Raised by `lakana_core.identity.sessions.require_step_up` for a session whose last passkey
    touch is missing or older than 15 minutes; a token caller never gets it.
    """


class NoPasskeyError(ConflictError):
    """A browser session asked for a passkey touch, and its person has no usable passkey.

    Raised by `lakana_core.identity.passkeys.PasskeyService.step_up_options` instead of
    answering with an empty list of passkeys, which would open the browser's picker with nothing
    to choose. Mapped to 409 through `ConflictError`, so the interface can say to add one.
    """


class ProviderSignInRequired(PermissionDenied):
    """A person linked to the identity provider tried a passkey way in (#32); mapped to 403.

    Raised by a passkey sign-in, a passkey registration and an administrator's recovery link of
    a linked person: they sign in only through the provider ("One way in").
    """


class ProviderStepUpRequired(ConflictError):
    """A linked person asked for a passkey step-up; mapped to 409 (#32).

    Raised by `lakana_core.identity.passkeys.PasskeyService.step_up_options`: a linked person
    confirms by signing in at the provider again (`POST /api/v1/sign-in/oidc/step-up/start`).
    """


class PasskeyRefused(AuthenticationError):
    """A passkey ceremony inside a browser session failed: a registration or a step-up touch.

    The browser's answer did not verify, its challenge was used or expired, or the passkey is
    not the caller's. Distinct from the plain `AuthenticationError` of a failed passkey sign-in
    (401): the caller is signed in, so the server answers 403 and the session goes on.
    """


_LINK_UNUSABLE = {
    "used": (
        "This sign-in link was already used, and each link works once. Sign in with your "
        "passkey, or ask whoever sent it for a new link."
    ),
    "expired": (
        "This sign-in link has expired. Ask whoever sent it for a new link, or sign in with "
        "your passkey."
    ),
    "withdrawn": (
        "This sign-in link was withdrawn. Ask whoever sent it for a new link, or sign in with "
        "your passkey."
    ),
    "unknown": (
        "This sign-in link is not known: check that you opened all of it, or ask whoever sent "
        "it for a new link."
    ),
}
"""The message for each state of a link that cannot be used."""


class LinkUnusableError(LakanaError):
    """A sign-in link cannot be used: it was used, expired, withdrawn, or is not known.

    A wrong secret is reported as ``unknown``, exactly like a public id that names no link, so
    a secret cannot be probed. The server answers 410 with `state`.

    Parameters
    ----------
    state
        ``used``, ``expired``, ``withdrawn`` or ``unknown``.
    """

    def __init__(self, state: Literal["used", "expired", "withdrawn", "unknown"]) -> None:
        self.state = state
        super().__init__(_LINK_UNUSABLE[state])


class OidcOff(ConflictError):
    """OIDC sign-in was asked for while it is off (no `LAKANA_OIDC__ISSUER`); mapped to 409."""


OidcRefusal = Literal[
    "not_linked",
    "deactivated",
    "token_invalid",
    "provider_unreachable",
    "provider_refused",
    "flow_expired",
    "identity_taken",
    "identity_mismatch",
    "link_unusable",
    "last_unlinked_admin",
    "cannot_link_operator",
    "already_linked",
    "auth_too_old",
]
"""Why an OIDC sign-in was refused; the values are stable."""

_OIDC_REFUSED: dict[str, str] = {
    "not_linked": (
        "Your account at the identity provider is not linked to anyone in Lakana. Ask an "
        "administrator for an invite."
    ),
    "deactivated": (
        "Your Lakana account is deactivated, so you can no longer sign in. If this is a "
        "mistake, ask an administrator for a new account."
    ),
    "token_invalid": (  # nosec B105 -- a sentence for a person, not a credential
        "Lakana could not verify the identity provider's answer, so nobody was signed in. Try "
        "again; if it keeps happening, tell your administrator."
    ),
    "provider_unreachable": (
        "Lakana could not reach the identity provider. Try again in a moment; if it keeps "
        "happening, tell your administrator."
    ),
    "provider_refused": (
        "The identity provider did not sign you in. Try again, or sign in at the identity "
        "provider first."
    ),
    "flow_expired": (
        "This sign-in was already used or took longer than 10 minutes. Start the sign-in again."
    ),
    "identity_taken": (
        "This account at the identity provider is already linked to another Lakana account, and "
        "one identity signs in as one account only. Sign in at the provider with your own "
        "account, or ask an administrator."
    ),
    "identity_mismatch": (
        "You signed in at the identity provider as someone else than the person this is for. "
        "Sign out at the provider, then sign in there with your own account and try again."
    ),
    "link_unusable": (
        "This link was used, withdrawn or expired while you were at the identity provider, so "
        "nothing was changed. Ask an administrator for a new link."
    ),
    "last_unlinked_admin": (
        "You are the last administrator who signs in with a passkey. Linking you would leave "
        "only administrators who need the identity provider, so an outage there would lock "
        "everyone out. Another administrator needs a passkey and no link first."
    ),
    "cannot_link_operator": (
        "This account cannot sign in through the identity provider: the server operator's "
        "account and service accounts never do."
    ),
    "already_linked": (
        "Your account is already linked to the identity provider. Sign in there; to link another "
        "identity, ask an administrator to unlink this one first."
    ),
    "auth_too_old": (
        "The identity provider did not confirm a fresh sign-in (it sent no auth_time, or an "
        "older one), so this was not confirmed. Try again; if it keeps happening, tell your "
        "administrator that the provider must send auth_time."
    ),
}


class OidcSignInRefused(PermissionDenied):
    """An OIDC sign-in was refused; mapped to 403 with `reason`.

    Parameters
    ----------
    reason
        ``not_linked``, ``deactivated``, ``token_invalid``, ``provider_unreachable``,
        ``provider_refused``, ``flow_expired``; while linking or confirming (#32, PR 2)
        ``identity_taken``, ``identity_mismatch``, ``link_unusable``, ``last_unlinked_admin``,
        ``cannot_link_operator``, ``already_linked`` or ``auth_too_old``.
    """

    def __init__(self, reason: OidcRefusal) -> None:
        self.reason = reason
        super().__init__(_OIDC_REFUSED[reason])


class CliGrantRefused(LakanaError):
    """A command-line sign-in's code, verifier or device code was refused; mapped to 400.

    The answer is the same for an unknown, used, expired or wrong value, so that none can be
    probed: the message says to start `lakana login` again.
    """


class CliPollTooFast(LakanaError):
    """A command line asked about its device grant faster than the interval; mapped to 429.

    The answer carries ``error: slow_down`` (RFC 8628's name) and a `Retry-After` header.
    """


class StorageUnavailableError(LakanaError):
    """Storage could not be written or read right now (a full disk, an unreachable bucket).

    Carries a text that names no path and none of storage's own words; the server maps it to
    503 with a `Retry-After` header, so a client may try again.
    """


class StorageRefusedError(LakanaError):
    """The storage answered, and refused the environment's credentials.

    Asking again does not help until the credentials are fixed, so the server maps it to 502
    without a `Retry-After` header, apart from `StorageUnavailableError`. The text names no path
    and none of storage's own words.
    """


class AuditUnavailableError(LakanaError):
    """A read of other people's activity was refused because its audit row could not be written.

    Raised by `lakana_core.audit.views.record_view` before any of the activity is returned (the
    view audit fails closed). The text names the cause by its kind only, never the database's own
    words; the server maps it to 503 with a `Retry-After` header.
    """


class GraphBusyError(LakanaError):
    """A metadata graph question ran past its time budget and was stopped.

    The server answers 503 with ``Retry-After``: the same question may fit when the server is
    less busy, and a narrower one fits sooner (`lakana_core.catalog.graph_model.BUSY`).
    """


class WriteOutcomeUnknownError(LakanaError):
    """A write's commit was not acknowledged and the table could not be asked what became of it.

    Raised by the catalog's write paths after the database failed on the way back from a commit
    and a fresh read of the table failed too. The write may have landed or not; its files are
    kept, because a deleted file a landed snapshot names breaks the table, while a kept file
    that turns out to be unreferenced is an orphan that `lakana table maintain` removes. The
    caller must not repeat the write blindly: an append could land twice. Mapped to 500 by the
    server (no mapped base class), and a permanent failure for a pipeline task, which therefore
    is not retried on its own.
    """


class UnsupportedTableError(LakanaError):
    """Lakana cannot read a catalog object as a table, or cannot read it correctly yet.

    Raised by `lakana_core.catalog.datafiles.list_data_files` when the current
    snapshot carries a live position- or equality-delete file: reading only
    the data files and ignoring the deletes would silently return rows the
    writer meant to remove. Refusing the read is the only correct behaviour
    until Lakana's query engine applies deletes itself, planned for M5.

    Also raised when SQL names an artifact, by the worker's source
    resolution and by `CatalogService.create_or_replace_view` for a view's
    reference: only a Python asset reads an artifact.
    """


class NotPublishedError(LakanaError):
    """A table cannot be read: its first build did not finish, or failed a blocking check.

    Raised by `CatalogService.load_table`, after the read grant was checked, for a table
    that is registered but not published (`Table.unpublished_run_id` is set): reading it
    would show an empty table, which a consumer cannot tell from "no data". The message
    names the check and the run. Mapped to 409 on the platform API and to 404 on the
    Iceberg REST catalog; a query that reads the table fails with it, and a task that does
    fails at once and is not retried.
    """


class KeptAfterCheckError(LakanaError):
    """A task cannot read a table: it was kept after a failed check and has not passed since.

    Raised by `CatalogService.load_table`, after the read grant was checked and after the
    not-published refusal, for a table that carries the note of a failed `keep` check
    (`Table.kept_run_id` is set) when the reader is a task (the principal has a run context)
    without standing: its run is not a run of the project that builds the table, or it is
    neither the table's own task, nor downstream of it in its run, nor the count of a check.
    A reader without a run context is never refused: the table keeps the failing data so that
    people can look at it. The message names the check, the run and the next step. Mapped to
    409 on the platform API and to 404 on the Iceberg REST catalog; a task that reads the
    table fails at once and is not retried.
    """


class CheckedSnapshotGoneError(LakanaError):
    """The snapshot a check result of a table names can no longer be read from it.

    Raised by `CatalogService.load_table_at_check`, after the read grant was checked and the
    result was found to be one of the named table, when the table's metadata no longer holds
    the snapshot (maintenance removed it), when the snapshot names no schema the table holds,
    or when the row is not a managed table with metadata. The text names the table, the
    result's run and the environment, and says where the number of rows remains
    (`lakana_core.runs.checks.checked_snapshot_gone_message`).

    Also raised by `lakana_core.catalog.datafiles.list_data_files` for a named snapshot its
    metadata does not hold, with a text that names no table; a caller that lists from what
    `load_table_at_check` returned never sees that one.
    """


class NoCheckedSnapshotError(LakanaError):
    """A check result names no snapshot, so there is nothing to read its rows from.

    A result has no checked snapshot when its table is a view, when the check's SQL does not
    read its own table, or when the check could not run. Raised by
    `CatalogService.load_table_at_check`, after the read grant was checked and the result was
    found to be one of the named table, with a text that names the run and gives no reason
    (`lakana_core.runs.checks.no_checked_snapshot_message`): the stored result does not say
    which of the three it was. Code that holds the check's definition tells them apart and
    words the reason and its next step (`no_snapshot_of_a_view_message`,
    `no_snapshot_not_read_message`, `no_snapshot_could_not_run_message`).
    """


class CheckResultGoneError(LakanaError):
    """The table a read names has no such check result.

    Raised by `CatalogService.load_table_at_check`, after the read grant was checked, when the
    result does not exist in the table's environment (it was removed with its run, or belongs
    to another environment), is of another table, names no table, or is of a table that was
    since dropped and built again under the name. All of these get one text, which names only
    the table the caller named (`lakana_core.runs.checks.check_result_gone_message`), so a
    caller cannot tell them apart and learns nothing of a result that is not this table's.
    """


class CheckNotInRunError(NotFoundError):
    """A run has no result for the check a caller named.

    Raised by `lakana_core.runs.check_rows.submit_check_rows`, after the caller was allowed
    to read the run and to submit a query, when none of that run's check results has the
    name. A not-found error, so the platform API answers 404; the text names the run and the
    command that lists its checks (`lakana_core.runs.checks.check_not_in_run_message`).
    """


class CheckSqlGoneError(ConflictError):
    """The SQL of a check can no longer be read from the project version its run used.

    Raised by `lakana_core.runs.check_rows.submit_check_rows` for a check result with a
    checked snapshot when the manifest of the run's project version holds no readable entry
    for the check: the result stores a count and never the SQL. A conflict, so the platform
    API answers 409; the text says where the count remains
    (`lakana_core.runs.checks.check_sql_gone_message`). Also for an entry whose SQL cannot be
    read, with its own text (`check_sql_unreadable_message`), which asks for a new deploy.
    """
