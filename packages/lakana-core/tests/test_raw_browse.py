"""Tests for browsing `raw/` before an external table is registered (#672)."""

from pathlib import Path

import pytest
from core_test_helpers import _config
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.external_location import claim_covers, parse_raw_folder
from lakana_core.catalog.raw_browse import RawEntry
from lakana_core.catalog.service import CatalogService
from lakana_core.config import ViewAuditConfig
from lakana_core.errors import (
    AuditUnavailableError,
    NotFoundError,
    PermissionDenied,
    StorageRefusedError,
    StorageUnavailableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType
from lakana_core.storage import FsspecStorage, StorageResolver

BRONZE = NamespacePath.from_parts(["bronze"])
SILVER = NamespacePath.from_parts(["silver"])


class _Rights(AllowAllAuthorization):
    """Permits only the listed actions on the listed places, named as text.

    Parameters
    ----------
    *rights
        ``(action, place)`` pairs; a place is ``"dev"``, a namespace (``"bronze"``) or a table
        (``"bronze.orders"``).
    """

    def __init__(self, *rights: tuple[Action, str]) -> None:
        self.rights = frozenset(rights)

    @staticmethod
    def _place(securable: Securable) -> str:
        """Name a securable as the rights name it.

        Parameters
        ----------
        securable
            The object.

        Returns
        -------
        str
            Its environment, namespace or dotted table.
        """
        if securable.kind is SecurableKind.ENVIRONMENT or securable.namespace is None:
            return str(securable.environment)
        if securable.table is None:
            return securable.namespace.joined()
        return f"{securable.namespace.joined()}.{securable.table}"

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer from the listed rights.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether the pair is listed.
        """
        return (action, self._place(securable)) in self.rights

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse what is not listed.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            If the pair is not listed.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied(f"No {action.value} on {self._place(securable)}.")


@pytest.fixture
def lake(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> Path:
    """Make `dev` with `bronze` and `silver`, files under `raw/`, and `bronze.orders` on one.

    Returns
    -------
    Path
        The storage root.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    # `bronze`, `silver` and `gold` come with every environment.
    for path, body in {
        "raw/orders/2026-11-01.csv": "id,amount\n1,2\n",
        "raw/feeds/returns/a.csv": "id\n1\n",
        "raw/feeds/payments/a.json": '{"id": 1}\n',
        "raw/stray.csv": "id\n1\n",
        "raw/a.csv": "id\n1\n",
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(body)
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=BRONZE, name="orders"),
        location_pattern="raw/orders/*.csv",
        format="csv",
    )
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=SILVER, name="payments"),
        location_pattern="raw/feeds/payments/*.json",
        format="json",
    )
    return tmp_path


def _browser(
    session: Session, *rights: tuple[Action, str], audit: ViewAuditConfig | None = None
) -> CatalogService:
    """Build a catalog that answers with the given rights.

    Returns
    -------
    CatalogService
        The service.
    """
    return CatalogService(session, StorageResolver(session), _Rights(*rights), view_audit=audit)


_MAY_REGISTER = (Action.CREATE_TABLE, "bronze")


def _views(session: Session) -> list[AuditEvent]:
    """Read the `ACTIVITY_VIEWED` rows, oldest first.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
            .order_by(AuditEvent.occurred_at)
        )
    )


@pytest.mark.parametrize(
    ("path", "folder"),
    [("raw", "raw"), ("raw/", "raw"), ("raw/orders", "raw/orders"), ("raw/a/b/", "raw/a/b")],
)
def test_parse_raw_folder_keeps_a_folder_under_raw(path: str, folder: str) -> None:
    assert parse_raw_folder(path) == folder


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/raw", "Path '/raw' must not start with '/'."),
        ("managed/x", "Path 'managed/x' must be 'raw' or a folder under 'raw/'."),
        ("rawish", "Path 'rawish' must be 'raw' or a folder under 'raw/'."),
        ("raw/../managed", "Path 'raw/../managed' has an invalid part '..'."),
        ("raw/./a", "Path 'raw/./a' has an invalid part '.'."),
        ("raw//a", "Path 'raw//a' has an invalid part ''."),
        ("raw/a\x00", "Path 'raw/a\\x00' contains a NUL byte."),
        ("raw/" + "x" * 1021, "Path is longer than 1024 characters."),
    ],
)
def test_parse_raw_folder_refuses_anything_outside_raw(path: str, message: str) -> None:
    with pytest.raises(ValidationError) as raised:
        parse_raw_folder(path)
    assert str(raised.value) == message


def test_a_claim_covers_itself_and_what_lies_below_it_only() -> None:
    assert claim_covers("raw/orders", "raw/orders")
    assert claim_covers("raw/orders", "raw/Orders/2026")
    assert not claim_covers("raw/feeds/payments", "raw/feeds")
    assert not claim_covers("raw/order", "raw/orders")


def test_without_the_right_to_register_anywhere_nothing_is_listed(
    session: Session, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading every table is not enough: browsing is the dialog's own right."""
    listed: list[str] = []
    monkeypatch.setattr(
        FsspecStorage,
        "list_children_page",
        lambda self, prefix, **kw: listed.append(prefix) or [],
    )
    browser = _browser(session, (Action.READ_TABLE, "bronze.orders"))
    with pytest.raises(PermissionDenied, match="No CREATE_TABLE on dev"):
        browser.list_raw(_reader(session), "dev")
    assert listed == []
    assert _views(session) == []


def test_a_right_to_register_in_one_namespace_lists_one_level_in_key_order(
    session: Session, lake: Path
) -> None:
    page = _browser(session, _MAY_REGISTER).list_raw(_reader(session), "dev")
    assert page.path == "raw"
    assert page.items == [
        RawEntry("a.csv", "file", 5, claimed=False, claimed_by=None),
        RawEntry("feeds", "folder", None, claimed=False, claimed_by=None),
        RawEntry("orders", "folder", None, claimed=True, claimed_by=None),
        RawEntry("stray.csv", "file", 5, claimed=False, claimed_by=None),
    ]
    assert page.next_cursor is None


def test_a_right_to_register_on_the_whole_environment_lets_a_reader_browse(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    page = _browser(session, (Action.CREATE_TABLE, "dev")).list_raw(_reader(session), "dev")
    assert page.items == []


def test_a_missing_environment_is_not_found_for_who_may_register_everywhere(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError):
        catalog.list_raw(principal, "nowhere")


def test_a_claim_names_its_table_only_to_who_may_read_it(session: Session, lake: Path) -> None:
    reader = _reader(session)
    shown = _browser(session, _MAY_REGISTER, (Action.READ_TABLE, "bronze.orders"))
    orders = {e.name: e for e in shown.list_raw(reader, "dev").items}["orders"]
    assert (orders.claimed, orders.claimed_by) == (True, "bronze.orders")

    inside = shown.list_raw(reader, "dev", path="raw/orders").items
    assert inside == [
        RawEntry("2026-11-01.csv", "file", 14, claimed=True, claimed_by="bronze.orders")
    ]


def test_a_folder_above_a_claim_stays_open_and_only_the_claimed_one_is_marked(
    session: Session, lake: Path
) -> None:
    """`raw/feeds` holds the claimed `payments`, and the free `returns` must stay reachable."""
    page = _browser(session, _MAY_REGISTER).list_raw(_reader(session), "dev", path="raw/feeds/")
    assert page.path == "raw/feeds"
    assert [(e.name, e.claimed) for e in page.items] == [("payments", True), ("returns", False)]


def test_pages_follow_a_cursor_and_a_bad_cursor_or_limit_is_refused(
    session: Session, lake: Path
) -> None:
    browser = _browser(session, _MAY_REGISTER)
    reader = _reader(session)
    first = browser.list_raw(reader, "dev", limit=2)
    assert [e.name for e in first.items] == ["a.csv", "feeds"]
    assert first.next_cursor is not None
    rest = browser.list_raw(reader, "dev", cursor=first.next_cursor, limit=2)
    assert [e.name for e in rest.items] == ["orders", "stray.csv"]
    assert rest.next_cursor is None
    with pytest.raises(ValidationError, match="limit must be between 1 and 200"):
        browser.list_raw(reader, "dev", limit=0)
    with pytest.raises(ValidationError, match="cursor is not valid"):
        browser.list_raw(reader, "dev", cursor="nonsense")


def test_a_path_outside_raw_is_refused_before_any_right_is_asked(
    session: Session, lake: Path
) -> None:
    with pytest.raises(ValidationError, match=r"invalid part '\.\.'"):
        _browser(session).list_raw(_reader(session), "dev", path="raw/../managed")


def test_each_folder_read_is_audited_once_per_window_as_a_view(
    session: Session, lake: Path
) -> None:
    browser = _browser(session, _MAY_REGISTER)
    reader = _reader(session)
    first = browser.list_raw(reader, "dev", limit=2)
    browser.list_raw(reader, "dev", cursor=first.next_cursor, limit=2)
    browser.list_raw(reader, "dev", path="raw/feeds")
    rows = _views(session)
    assert [(r.principal_id, r.securable_kind, r.securable_environment) for r in rows] == [
        (reader.id, "ENVIRONMENT", "dev"),
        (reader.id, "ENVIRONMENT", "dev"),
    ]
    assert rows[0].detail == {
        "view": "raw_files",
        "filters": {"path": "raw"},
        "rows_shown": 2,
        "others_shown": 0,
        "distinct_others": 0,
        "targeting_key": {"path": "raw"},
    }
    assert rows[1].detail["targeting_key"] == {"path": "raw/feeds"}


def test_a_read_that_cannot_be_audited_returns_nothing(session: Session, lake: Path) -> None:
    session.execute(
        text(
            "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
            "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
    )
    session.commit()
    with pytest.raises(AuditUnavailableError):
        _browser(session, _MAY_REGISTER).list_raw(_reader(session), "dev")


def test_the_view_audit_setting_turns_the_row_off(session: Session, lake: Path) -> None:
    browser = _browser(session, _MAY_REGISTER, audit=ViewAuditConfig(enabled=False))
    assert browser.list_raw(_reader(session), "dev").items
    assert _views(session) == []


@pytest.mark.parametrize(
    ("error", "raised", "words"),
    [
        (PermissionError("denied"), StorageRefusedError, "refused access"),
        (ConnectionError("down"), StorageUnavailableError, "could not be reached"),
    ],
)
def test_a_storage_that_refuses_or_does_not_answer_names_the_next_step(
    session: Session,
    lake: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raised: type[Exception],
    words: str,
) -> None:
    def fail(self: FsspecStorage, prefix: str, **kw: object) -> None:
        raise error

    monkeypatch.setattr(FsspecStorage, "list_children_page", fail)
    with pytest.raises(raised) as caught:
        _browser(session, _MAY_REGISTER).list_raw(_reader(session), "dev")
    assert str(caught.value).startswith(f"The storage of environment 'dev' {words}")
    assert _views(session) == []


def test_any_other_storage_error_is_raised_as_it_was(
    session: Session, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: FsspecStorage, prefix: str, **kw: object) -> None:
        raise RuntimeError("odd")

    monkeypatch.setattr(FsspecStorage, "list_children_page", fail)
    with pytest.raises(RuntimeError, match="odd"):
        _browser(session, _MAY_REGISTER).list_raw(_reader(session), "dev")


def _reader(session: Session) -> PrincipalRef:
    """Persist the person who browses, once.

    Returns
    -------
    PrincipalRef
        The reader.
    """
    return ensure_bootstrap_principal(session, "ada")
