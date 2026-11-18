"""The `connections` resource: `Client.connections`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, every_page, query_params, route_of_a_newer_server
from lakana._models import (
    Connection,
    ConnectionEnvironment,
    ConnectionPage,
    ConnectionTest,
    DeclaringAsset,
    LastConnectionTest,
    QueuedConnectionTest,
    QueuedConnectionTestPage,
)
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _object

_LIST_ALL_INSTEAD = "list one environment with `lakana connection list --env ENV`"
"""What to do instead of `Connections.list_all` against a server without that route."""

_ENVIRONMENTS_INSTEAD = "see the environments with `lakana env list`"
"""What to do instead of `Connections.environments` against a server without that route."""

_TEST_ALL_INSTEAD = "test one connection at a time with `lakana connection test NAME --env ENV`"
"""What to do instead of `Connections.queue_tests` against a server without that route."""

_EnvironmentList = list[ConnectionEnvironment]
_Tests = dict[str, ConnectionTest]


def _when(value: object) -> datetime | None:
    """Read a time that may be `null`.

    Parameters
    ----------
    value
        An ISO 8601 text, or `None`.

    Returns
    -------
    datetime or None
        The time, or `None`.

    Raises
    ------
    TypeError
        If `value` is neither text nor `None`.
    ValueError
        If the text is not a time.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("a time is text")
    return datetime.fromisoformat(value)


def _last_test_from_json(data: object) -> LastConnectionTest | None:
    """Build a `LastConnectionTest` from the `last_test` of a `ConnectionOut` body.

    The fields a reader without `MANAGE_CONNECTION` does not get are read as `None`.

    Parameters
    ----------
    data
        The decoded `last_test`, or `None` for a connection never tested.

    Returns
    -------
    LastConnectionTest or None
        The test, or `None`.

    Raises
    ------
    AssertionError, KeyError, TypeError, ValueError
        If `data` is not an object with a state; `_connection_from_json` turns each into
        `InvalidResponse`.
    """
    if data is None:
        return None
    assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; the caller catches it
    test_id = data.get("id")
    return LastConnectionTest(
        state=str(data["state"]),
        outcome=data.get("outcome"),
        finished_at=_when(data.get("finished_at")),
        id=None if test_id is None else str(test_id),
        created_at=_when(data.get("created_at")),
        message=data.get("message"),
        requested_by=data.get("requested_by"),
    )


def _connection_from_json(data: object) -> Connection:
    """Build a `Connection` from a `ConnectionOut` body.

    A field this SDK does not know is ignored; `environment`, `last_test` and `actions`, which
    an older server does not send, are read as `None`, `None` and no actions when missing.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Connection
        The connection.

    Raises
    ------
    InvalidResponse
        If `data` is not an object with every field of a connection.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        address, settings, declared = data["address"], data["settings"], data["declared_by"]
        assert isinstance(address, dict) and isinstance(settings, dict)  # nosec B101
        assert isinstance(declared, list)  # nosec B101 -- as above
        actions = data.get("actions", [])
        assert isinstance(actions, list)  # nosec B101 -- as above
        return Connection(
            name=data["name"],
            kind=data["kind"],
            address=address,
            settings=settings,
            secret=data.get("secret"),
            declared_by=[
                DeclaringAsset(repository=item["repository"], asset=item["asset"])
                for item in declared
            ],
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            environment=data.get("environment"),
            last_test=_last_test_from_json(data.get("last_test")),
            actions=tuple(str(action) for action in actions),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a connection")) from None


def _connection_test_from_json(data: object) -> ConnectionTest:
    """Build a `ConnectionTest` from a `ConnectionTestOut` body.

    A field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    ConnectionTest
        The test.

    Raises
    ------
    InvalidResponse
        If `data` is not an object with every field of a test.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        finished = data["finished_at"]
        return ConnectionTest(
            id=str(data["id"]),
            state=str(data["state"]),
            outcome=data["outcome"],
            message=data["message"],
            created_at=datetime.fromisoformat(data["created_at"]),
            finished_at=None if finished is None else datetime.fromisoformat(finished),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a connection test")) from None


def _page_from_json(data: dict[str, Any]) -> ConnectionPage:
    """Build a `ConnectionPage` from a `ConnectionPage` body.

    Parameters
    ----------
    data
        The decoded object.

    Returns
    -------
    ConnectionPage
        The page.

    Raises
    ------
    InvalidResponse
        If the object has no list of items or its cursor is not text.
    """
    items, cursor = data.get("items"), data.get("next_cursor")
    if not isinstance(items, list) or not (cursor is None or isinstance(cursor, str)):
        raise InvalidResponse(_ODD_ANSWER.format(what="the connections"))
    return ConnectionPage(items=[_connection_from_json(item) for item in items], next_cursor=cursor)


def _environments_from_json(data: object) -> list[ConnectionEnvironment]:
    """Build the `ConnectionEnvironment` list from a `ConnectionEnvironmentOut` list body.

    A field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    list of ConnectionEnvironment
        The environments, in the server's order.

    Raises
    ------
    InvalidResponse
        If `data` is not a list of objects, each with a name and a list of actions.
    """
    try:
        assert isinstance(data, list)  # nosec B101 -- an untrusted answer; caught just below
        found = []
        for item in data:
            actions = item["actions"]
            assert isinstance(actions, list)  # nosec B101 -- as above
            found.append(
                ConnectionEnvironment(
                    name=str(item["name"]), actions=tuple(str(action) for action in actions)
                )
            )
        return found
    except (AssertionError, KeyError, TypeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the environments")) from None


def _queued_page_from_json(data: dict[str, Any]) -> QueuedConnectionTestPage:
    """Build a `QueuedConnectionTestPage` from a `ConnectionQueuedTestPage` body.

    A field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded object.

    Returns
    -------
    QueuedConnectionTestPage
        The page.

    Raises
    ------
    InvalidResponse
        If the object has no list of items, an item lacks a field, or its cursor is not text.
    """
    try:
        items, cursor = data["items"], data["next_cursor"]
        assert isinstance(items, list)  # nosec B101 -- an untrusted answer; caught just below
        assert cursor is None or isinstance(cursor, str)  # nosec B101 -- as above
        return QueuedConnectionTestPage(
            items=[
                QueuedConnectionTest(
                    connection=str(item["connection"]),
                    test_id=str(item["test_id"]),
                    queued=bool(item["queued"]),
                )
                for item in items
            ],
            next_cursor=cursor,
        )
    except (AssertionError, KeyError, TypeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the queued tests")) from None


def _still(environment: str, name: str, test: ConnectionTest, timeout: float) -> str:
    """Write the message of a test that is not done in time.

    Parameters
    ----------
    environment
        The environment.
    name
        The connection's name.
    test
        The test as last seen.
    timeout
        The seconds waited.

    Returns
    -------
    str
        What state it is in, who runs tests (queued) or which worker log to read (running), and
        how to look again.
    """
    again = f"Look again with lakana connection test {name} --env {environment} --id {test.id}"
    if test.state == "queued":
        found = (
            "no worker has taken it. A worker of either runner runs connection tests: check "
            "that one is running (`docker compose ps worker`, or the `lakana local` terminal). "
            f"{again}"
        )
    else:
        found = (
            "a worker took it; read the worker's log (`docker compose logs worker`, or the "
            f"`lakana local` terminal). {again}"
        )
    return f"the test of `{name}` is still {test.state} after {timeout:g}s: {found}"


def _still_all(environment: str, open_tests: Mapping[str, ConnectionTest], timeout: float) -> str:
    """Write the message of a Test all whose tests are not all done in time.

    Parameters
    ----------
    environment
        The environment.
    open_tests
        Each connection whose test is not done, to its test as last seen.
    timeout
        The seconds waited.

    Returns
    -------
    str
        Each open connection's state and how to look at its test again, then who runs tests
        (for a queued one) and which worker log to read (for a running one); for a single
        connection, the message of a single test.
    """
    if len(open_tests) == 1:
        ((name, test),) = open_tests.items()
        return _still(environment, name, test, timeout)
    each = ", ".join(
        f"`{name}` is still {test.state} (look again with lakana connection test {name} "
        f"--env {environment} --id {test.id})"
        for name, test in sorted(open_tests.items())
    )
    states = {test.state for test in open_tests.values()}
    hints = []
    if "queued" in states:
        hints.append(
            "No worker has taken a queued test: check that a worker of either runner is running "
            "(`docker compose ps worker`, or the `lakana local` terminal)."
        )
    if states - {"queued"}:
        hints.append(
            "A worker took a running test: read the worker's log (`docker compose logs worker`, "
            "or the `lakana local` terminal)."
        )
    return (
        f"{len(open_tests)} connection tests are not done after {timeout:g}s: {each}. "
        + " ".join(hints)
    )


class Connections:
    """The `connections` resource of the platform API: the connections of an environment.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment: str,
        name: str,
        *,
        kind: str,
        address: Mapping[str, Any],
        settings: Mapping[str, Any] | None = None,
        secret: str | None = None,
        secret_values: Mapping[str, str] | None = None,
    ) -> Connection:
        """Create a connection (needs `MANAGE_CONNECTION` in the environment).

        Parameters
        ----------
        environment
            The environment.
        name
            Lower-case letters, digits and ``-``, starting with a letter; never changes.
        kind
            ``postgresql``, ``mariadb``, ``sqlite-file``, ``http``, ``s3``, ``gcs``, ``azure``,
            ``directory`` or ``tcp``; never changes.
        address
            Where the source is, for example ``{"host": "db.example.com", "database": "crm"}``.
        settings
            How to reach it, for example ``{"user": "reader"}``; none by default.
        secret
            The name of an existing secret of the environment to use. Naming one that is not
            bound to this connection needs `MANAGE_SECRET`.
        secret_values
            Values of a new secret ``<name>-connection`` (or the next free
            ``<name>-connection-N``), born bound to the connection, for
            example ``{"password": ...}``. Sent once, in the request body; never shown again.

        Returns
        -------
        Connection
            The created connection.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without the right.
        lakana.errors.Conflict
            If the name is taken, or every secret name up to ``<name>-connection-100`` is.
        lakana.errors.ValidationError
            For a field that is not valid; the message names it.
        """
        response = self._transport.request(
            ROUTES["connections.create"],
            path_params={"environment_name": environment},
            json={
                "name": name,
                "kind": kind,
                "address": dict(address),
                "settings": dict(settings or {}),
                "secret": secret,
                "secret_values": None if secret_values is None else dict(secret_values),
            },
        )
        return _connection_from_json(_object(response, "a connection"))

    def list(
        self, environment: str, *, limit: int | None = None, cursor: str | None = None
    ) -> ConnectionPage:
        """List one page of an environment's connections, newest first.

        Parameters
        ----------
        environment
            The environment.
        limit
            Most connections returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`.

        Returns
        -------
        ConnectionPage
            The connections, and the cursor of the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["connections.list"],
            path_params={"environment_name": environment},
            params=query_params(limit=limit, cursor=cursor),
        )
        return _page_from_json(_object(response, "the connections"))

    def iter(self, environment: str, *, page_size: int = 100) -> Iterator[Connection]:
        """Yield every connection of an environment, newest first, fetching pages as needed.

        Parameters
        ----------
        environment
            The environment.
        page_size
            Connections per request (1 to 200).

        Returns
        -------
        Iterator of Connection
            Each connection, once.
        """
        return every_page(lambda cursor: self.list(environment, limit=page_size, cursor=cursor))

    def list_all(
        self, *, environment: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> ConnectionPage:
        """List one page of the connections of every environment you may read, newest first.

        The environments are those where you hold `READ_ENVIRONMENT`; one where you do not is
        left out, so this is never refused for a missing right. Each connection names its
        `environment`. With `environment`, this is `list` of that environment.

        Parameters
        ----------
        environment
            Only this environment; then a missing right or environment is refused as by `list`.
        limit
            Most connections returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor` of this list.

        Returns
        -------
        ConnectionPage
            The connections, and the cursor of the next page (`None` on the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            With a task's token; with `environment`, also if you may not read it.
        lakana.errors.NotFound
            With `environment`, if it does not exist; or, with a sentence saying so, if the
            server is older than this package and has no `GET /api/v1/connections`.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200 or `cursor` is not one the server issued for this
            list.
        """
        with route_of_a_newer_server("GET /api/v1/connections", _LIST_ALL_INSTEAD):
            response = self._transport.request(
                ROUTES["connections.list_all"],
                params=query_params(environment=environment, limit=limit, cursor=cursor),
            )
        return _page_from_json(_object(response, "the connections"))

    def iter_all(
        self, *, environment: str | None = None, page_size: int = 100
    ) -> Iterator[Connection]:
        """Yield every connection of every environment you may read, fetching pages as needed.

        Parameters
        ----------
        environment
            Only this environment.
        page_size
            Connections per request (1 to 200).

        Returns
        -------
        Iterator of Connection
            Each connection, once, in the order of `list_all`.
        """
        return every_page(
            lambda cursor: self.list_all(environment=environment, limit=page_size, cursor=cursor)
        )

    def environments(self) -> _EnvironmentList:
        """List the environments whose connections you may read, sorted by name.

        Returns
        -------
        list of ConnectionEnvironment
            Each one's name and what you may do there; empty when there is none.

        Raises
        ------
        lakana.errors.PermissionDenied
            With a task's token.
        lakana.errors.NotFound
            With a sentence saying so, if the server is older than this package and has no
            `GET /api/v1/connections/environments`.
        lakana.errors.InvalidResponse
            If the answer is not a list of environments.
        """
        with route_of_a_newer_server("GET /api/v1/connections/environments", _ENVIRONMENTS_INSTEAD):
            response = self._transport.request(ROUTES["connections.environments"])
        try:
            body = response.json()
        except ValueError:
            raise InvalidResponse(_ODD_ANSWER.format(what="the environments")) from None
        return _environments_from_json(body)

    def get(self, environment: str, name: str) -> Connection:
        """Show a connection.

        Parameters
        ----------
        environment
            The environment.
        name
            The connection's name.

        Returns
        -------
        Connection
            The connection.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the connection does not exist.
        """
        response = self._transport.request(
            ROUTES["connections.get"],
            path_params={"environment_name": environment, "connection_name": name},
        )
        return _connection_from_json(_object(response, "a connection"))

    # The parameters mirror `lakana_core.connections.service.ConnectionService.update`, which
    # this package cannot import (the SDK depends on no other Lakana package).
    # jscpd:ignore-start
    def update(
        self,
        environment: str,
        name: str,
        *,
        address: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        secret: str | None = None,
        secret_values: Mapping[str, str] | None = None,
        clear: Iterable[str] = (),
    ) -> Connection:
        # jscpd:ignore-end
        """Change a connection; only what is given changes.

        Parameters
        ----------
        environment
            The environment.
        name
            The connection's name.
        address
            Address keys to change; a key set to `None` is removed.
        settings
            Settings keys to change; a key set to `None` is removed.
        secret
            The name of a secret of the environment to use instead.
        secret_values
            New values: they set the connection's own secret again, or create
            ``<name>-connection`` (or the next free name) when it has none. Sent once, in the
            request body.
        clear
            ``secret`` (stop using the secret and unbind it) and settings keys to remove.

        Returns
        -------
        Connection
            The changed connection.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without the right; a change that could send the secret somewhere new also needs
            `MANAGE_SECRET`, unless it gives new values.
        lakana.errors.ValidationError
            If nothing changes or the result is not valid.
        """
        changes: dict[str, Any] = {
            key: dict(value)
            for key, value in (
                ("address", address),
                ("settings", settings),
                ("secret_values", secret_values),
            )
            if value is not None
        }
        if secret is not None:
            changes["secret"] = secret
        cleared = list(clear)
        if cleared:
            changes["clear"] = cleared
        response = self._transport.request(
            ROUTES["connections.update"],
            path_params={"environment_name": environment, "connection_name": name},
            json=changes,
        )
        return _connection_from_json(_object(response, "a connection"))

    def delete(self, environment: str, name: str) -> None:
        """Delete a connection; its secret is unbound and stays in the environment.

        Parameters
        ----------
        environment
            The environment.
        name
            The connection's name.

        Raises
        ------
        lakana.errors.Conflict
            While a deployed project version declares it; the message names the asset.
        """
        self._transport.request(
            ROUTES["connections.delete"],
            path_params={"environment_name": environment, "connection_name": name},
        )

    def test(
        self,
        environment: str,
        name: str,
        *,
        wait: bool = True,
        timeout: float = 120.0,
        test_id: str | None = None,
    ) -> ConnectionTest:
        """Test a connection: a worker checks that its host can be reached.

        Needs `MANAGE_CONNECTION` in the environment. Queues a test (or, with `test_id`, looks at
        an earlier one) and, with `wait`, polls it with a growing pause (0.5 s up to 2 s) until
        it is done.

        Parameters
        ----------
        environment
            The environment.
        name
            The connection's name.
        wait
            Poll until the test is done; `False` returns it as it is (usually ``queued``).
        timeout
            Seconds to keep polling.
        test_id
            Look at this earlier test instead of queueing a new one.

        Returns
        -------
        ConnectionTest
            The test; ``state == "done"`` when waited for, with its `outcome`.

        Raises
        ------
        lakana.errors.Conflict
            If a test of this connection is already queued or running.
        lakana.errors.NotFound
            If the environment, the connection or the test does not exist.
        TimeoutError
            If the test is not done after `timeout` seconds; the message says whether a worker
            took it and how to look again.
        """
        path_params = {"environment_name": environment, "connection_name": name}
        if test_id is None:
            response = self._transport.request(ROUTES["connections.test"], path_params=path_params)
            test = _connection_test_from_json(_object(response, "a connection test"))
        else:
            test = self._read_test(environment, name, test_id)
        if not wait:
            return test
        return self._until_done(
            environment,
            {name: test},
            timeout,
            lambda open_tests: _still(environment, name, open_tests[name], timeout),
        )[name]

    def queue_tests(
        self, environment: str, *, limit: int | None = None, cursor: str | None = None
    ) -> QueuedConnectionTestPage:
        """Queue a test of every connection on one page of an environment's list (Test all).

        Needs `MANAGE_CONNECTION` in the environment. The page is the one `list` gives for the
        same `limit` and `cursor`, newest first. A connection whose test is queued or running and
        under 10 minutes old is not queued again: its open test is named, with ``queued`` false,
        so this is never refused for an open test. `test_all` walks every page and waits.

        Parameters
        ----------
        environment
            The environment.
        limit
            Most connections tested (1 to 200); `None` leaves it to the server (200).
        cursor
            A previous page's `next_cursor`, of this call or of `list`.

        Returns
        -------
        QueuedConnectionTestPage
            A test for each connection of the page, and the cursor of the next (`None` on the
            last).

        Raises
        ------
        lakana.errors.PermissionDenied
            Without `MANAGE_CONNECTION` in the environment.
        lakana.errors.Conflict
            If a connection changed at the same time; the ones before it stay queued, and
            calling again returns their tests as already open.
        lakana.errors.NotFound
            If the environment does not exist; or, with a sentence saying so, if the server is
            older than this package and has no Test all.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200 or `cursor` is not one the server issued for the
            environment's connection list.
        """
        with route_of_a_newer_server(
            "POST /api/v1/environments/{environment_name}/connection-tests", _TEST_ALL_INSTEAD
        ):
            response = self._transport.request(
                ROUTES["connections.test_all"],
                path_params={"environment_name": environment},
                params=query_params(limit=limit, cursor=cursor),
            )
        return _queued_page_from_json(_object(response, "the queued tests"))

    def test_all(
        self, environment: str, *, wait: bool = True, timeout: float = 120.0
    ) -> dict[str, ConnectionTest]:
        """Test every connection of an environment, and wait for every test.

        Calls `queue_tests` for every page, then reads each test, polling with the growing pause
        of `test` (0.5 s up to 2 s) until every one is done. A connection that already had an
        open test gets that test, not a new one.

        Parameters
        ----------
        environment
            The environment.
        wait
            Poll until every test is done; `False` reads each test once.
        timeout
            Seconds to keep polling, for all of them together.

        Returns
        -------
        dict of str to ConnectionTest
            Each connection's name to its test, newest connection first; empty when the
            environment has none.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without `MANAGE_CONNECTION` in the environment.
        lakana.errors.Conflict
            If a connection changed at the same time; the ones before it stay queued, and
            calling again returns their tests as already open.
        lakana.errors.NotFound
            If the environment does not exist, or the server has no Test all.
        TimeoutError
            If a test is not done after `timeout` seconds; the message names every connection
            still open and how to look at its test again.
        """
        queued = list(every_page(lambda cursor: self.queue_tests(environment, cursor=cursor)))
        tests = {
            item.connection: self._read_test(environment, item.connection, item.test_id)
            for item in queued
        }
        if not wait:
            return tests
        return self._until_done(
            environment,
            tests,
            timeout,
            lambda open_tests: _still_all(environment, open_tests, timeout),
        )

    def _read_test(self, environment: str, name: str, test_id: str) -> ConnectionTest:
        """Read a test of a connection.

        Parameters
        ----------
        environment
            The environment.
        name
            The connection's name.
        test_id
            The test's id.

        Returns
        -------
        ConnectionTest
            The test as it is now.
        """
        response = self._transport.request(
            ROUTES["connections.test_get"],
            path_params={
                "environment_name": environment,
                "connection_name": name,
                "test_id": test_id,
            },
        )
        return _connection_test_from_json(_object(response, "a connection test"))

    def _until_done(
        self,
        environment: str,
        tests: _Tests,
        timeout: float,
        still: Callable[[_Tests], str],
    ) -> _Tests:
        """Read the tests that are not done, with a growing pause, until every one is.

        Parameters
        ----------
        environment
            The environment.
        tests
            Each connection's name to its test as last read.
        timeout
            Seconds to keep polling.
        still
            Writes the timeout's message from the tests that are not done.

        Returns
        -------
        dict of str to ConnectionTest
            The same connections, in the same order, each with its done test.

        Raises
        ------
        TimeoutError
            If a test is not done after `timeout` seconds, with `still`'s message.
        """
        tests = dict(tests)
        deadline = time.monotonic() + timeout
        interval = 0.5
        while open_tests := {name: t for name, t in tests.items() if t.state != "done"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(still(open_tests))
            time.sleep(interval)
            interval = min(interval * 2, 2.0)
            for name, test in open_tests.items():
                tests[name] = self._read_test(environment, name, test.id)
        return tests
