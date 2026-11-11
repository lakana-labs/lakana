"""Two sessions on a real PostgreSQL: the locks serialise a bind and a naming or URL change."""

from __future__ import annotations

import base64
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from unittest.mock import patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    Base,
    Connection,
    Environment,
    PackageIndex,
    Principal,
    PrincipalType,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.projects.deploy_keys import DeployKeyService
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets import service as secret_service_module
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretBindingRefused, SecretUse

pytestmark = pytest.mark.integration

_KEYS = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
_WAIT_SECONDS = 2.0
_JOIN_SECONDS = 60.0


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres and create the schema.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


class _World:
    """Names and ids of one test's rows; names are unique so tests share a database."""

    def __init__(self, engine: Engine) -> None:
        tag = uuid.uuid4().hex[:8]
        self.engine = engine
        self.env = f"env-{tag}"
        self.secret = f"pypi-{tag}"
        self.a = f"a-{tag}"
        self.b = f"b-{tag}"
        with Session(engine) as session:
            session.add(
                Environment(id=uuid.uuid4(), name=self.env, storage_root=f"file:///srv/{tag}")
            )
            session.commit()
            self.admin = _principal(session, f"admin-{tag}")
            self.manager = _principal(session, f"manager-{tag}")
            grants = GrantService(session, AllowAllAuthorization())
            grants.grant(
                self.admin, principal=self.admin.name, role=Role.ADMIN, kind=SecurableKind.INSTANCE
            )
            grants.grant(
                self.admin,
                principal=self.manager.name,
                action=Action.MANAGE_PACKAGE_INDEX,
                kind=SecurableKind.INSTANCE,
            )
            indexes = PackageIndexService(session, AllowAllAuthorization())
            row_a = indexes.create(self.admin, name=self.a, url=f"https://{self.a}.example/s")
            row_b = indexes.create(self.admin, name=self.b, url=f"https://{self.b}.example/s")
            self.use_a = SecretUse.package_index(row_a)
            self.use_b = SecretUse.package_index(row_b)
            self.secret_id = (
                SecretService(session, AllowAllAuthorization(), _KEYS)
                .create(
                    self.admin,
                    environment=self.env,
                    name=self.secret,
                    values={"password": "pw"},
                    use=self.use_a,
                )
                .id
            )


def _race(
    engine: Engine, first: Callable[[Session], None], second: Callable[[Session], None]
) -> BaseException | None:
    """Run `first` and commit it, with `second` started just before the commit.

    `second` runs in a thread on its own session and has `_WAIT_SECONDS` to reach the lock
    `first` holds; the commit then lets it go on.

    Parameters
    ----------
    engine
        The engine both sessions use.
    first
        Work of the first session; the helper commits after it.
    second
        Work of the second session, in a thread.

    Returns
    -------
    BaseException or None
        What `second` raised, or `None`.
    """
    outcome: list[BaseException | None] = []
    with Session(engine) as one, Session(engine) as two:

        def _run() -> None:
            try:
                second(two)
                outcome.append(None)
            except BaseException as exc:
                outcome.append(exc)

        worker = threading.Thread(target=_run, daemon=True)

        def _start(_session: Session) -> None:
            worker.start()
            time.sleep(_WAIT_SECONDS)

        first(one)
        event.listen(one, "before_commit", _start, once=True)
        one.commit()
        worker.join(_JOIN_SECONDS)
        assert not worker.is_alive(), "the second session never finished"
    return outcome[0]


def test_naming_a_secret_waits_for_a_bind_and_sees_it(engine: Engine) -> None:
    """The bind to `b` commits first; naming the secret for `a` then finds it bound elsewhere."""
    world = _World(engine)

    def _second(session: Session) -> None:
        stale = session.get(Secret, world.secret_id)  # held, so it stays in the identity map
        assert stale is not None
        assert stale.use_target_id == world.use_a.target_id
        PackageIndexService(session, AllowAllAuthorization()).update(
            world.admin, world.a, {"credential_secret": world.secret}
        )

    outcome = _race(engine, lambda one: _bind_without_commit(one, world, world.use_b), _second)

    assert isinstance(outcome, SecretBindingRefused), outcome
    with Session(engine) as session:
        secret = session.get(Secret, world.secret_id)
        assert secret is not None
        assert (secret.use_kind, secret.use_target_id) == (
            SecretUseKind.PACKAGE_INDEX,
            world.use_b.target_id,
        )
        assert (
            session.execute(
                select(PackageIndex.credential_secret).where(PackageIndex.name == world.a)
            ).scalar_one()
            is None
        )


def _bind_without_commit(session: Session, world: _World, use: SecretUse) -> None:
    """Do what `SecretService.bind` does up to its commit: take the locks and bind to `use`.

    `bind` commits itself; here its commit is swallowed, so that the race helper's commit is
    the one that releases the locks after the second session has queued behind them.

    Parameters
    ----------
    session
        The first session.
    world
        The test's rows.
    use
        The use to bind the secret to.
    """
    service = SecretService(session, AllowAllAuthorization(), _KEYS)
    with patch.object(session, "commit"):
        service.bind(world.admin, world.secret_id, use)


def test_a_url_change_waits_for_a_bind_and_is_refused(engine: Engine) -> None:
    """The bind to `a` commits first; the manager's move of `a` then sees a bound secret."""
    world = _World(engine)
    with Session(engine) as session:
        # Start from an unbound secret: the fixture's secret is bound to `a` already.
        secret = session.get(Secret, world.secret_id)
        assert secret is not None
        secret.use_kind, secret.use_target_id = None, None
        session.commit()

    def _move(session: Session) -> None:
        PackageIndexService(session, GrantAuthorization(session)).update(
            world.manager, world.a, {"url": f"https://evil-{world.a}.example/s"}
        )

    outcome = _race(engine, lambda one: _bind_without_commit(one, world, world.use_a), _move)

    assert isinstance(outcome, SecretBindingRefused), outcome
    with Session(engine) as session:
        assert (
            session.execute(
                select(PackageIndex.url).where(PackageIndex.name == world.a)
            ).scalar_one()
            == f"https://{world.a}.example/s"
        )


def test_two_moves_between_the_same_connections_in_opposite_directions_both_commit(
    engine: Engine,
) -> None:
    """S1 moves from A to B while S2 moves from B to A: no deadlock, no 409, rows agree.

    Each bind waits at a barrier right after it locked its entry. Taking both connections in
    one order first (`lock_connections`) makes the second bind queue before the barrier, so
    the barrier times out and the binds run one after the other; without that order each would
    hold one connection and wait for the other.
    """
    tag = uuid.uuid4().hex[:8]
    with Session(engine) as session:
        session.add(Environment(id=uuid.uuid4(), name=f"env-{tag}", storage_root="file:///srv/c"))
        session.commit()
        admin = _principal(session, f"admin-{tag}")
        connections = ConnectionService(session, AllowAllAuthorization(), _KEYS)
        for name in (f"a-{tag}", f"b-{tag}"):
            connections.create(
                admin,
                f"env-{tag}",
                name=name,
                kind=ConnectionKind.POSTGRESQL,
                address={"host": "db.example.com", "database": "crm"},
                settings={"user": "reader"},
                secret_values={"password": "pw"},
            )
        rows = {
            row.name: row
            for row in session.execute(
                select(Connection).where(Connection.name.in_([f"a-{tag}", f"b-{tag}"]))
            ).scalars()
        }
        first, second = rows[f"a-{tag}"], rows[f"b-{tag}"]
        moves = [
            (first.secret_id, SecretUse(SecretUseKind.CONNECTION, second.id, second.name)),
            (second.secret_id, SecretUse(SecretUseKind.CONNECTION, first.id, first.name)),
        ]
        ids = {"a": first.id, "b": second.id, "s1": first.secret_id, "s2": second.secret_id}

    barrier = threading.Barrier(2)
    original = secret_service_module.lock_entry

    def _lock_then_meet(session: Session, use: SecretUse, environment: Environment) -> SecretUse:
        """Lock the entry, then wait for the other bind at the barrier, at most a while.

        Parameters
        ----------
        session
            Database session.
        use
            The use whose entry to lock.
        environment
            The secret's environment.

        Returns
        -------
        SecretUse
            What `lock_entry` returned.
        """
        locked = original(session, use, environment)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=_WAIT_SECONDS)
        return locked

    outcomes: list[BaseException | None] = [None, None]

    def _move(index: int) -> None:
        """Run one bind on its own session.

        Parameters
        ----------
        index
            Which move.
        """
        secret_id, use = moves[index]
        assert secret_id is not None
        try:
            with Session(engine) as own:
                SecretService(own, AllowAllAuthorization(), _KEYS).bind(admin, secret_id, use)
        except BaseException as exc:
            outcomes[index] = exc

    with patch.object(secret_service_module, "lock_entry", _lock_then_meet):
        workers = [threading.Thread(target=_move, args=(i,), daemon=True) for i in (0, 1)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(_JOIN_SECONDS)
    assert not any(worker.is_alive() for worker in workers), "a bind never finished"

    assert outcomes == [None, None]
    with Session(engine) as session:
        a, b = session.get(Connection, ids["a"]), session.get(Connection, ids["b"])
        s1, s2 = session.get(Secret, ids["s1"]), session.get(Secret, ids["s2"])
        assert a is not None and b is not None and s1 is not None and s2 is not None
        assert (a.secret_id, b.secret_id) == (ids["s2"], ids["s1"])
        assert (s1.use_kind, s1.use_target_id) == (SecretUseKind.CONNECTION, ids["b"])
        assert (s2.use_kind, s2.use_target_id) == (SecretUseKind.CONNECTION, ids["a"])


def test_a_key_revoked_while_it_is_replaced_is_not_rotated(engine: Engine) -> None:
    """The revoke commits first; the replacement then sees a revoked key and writes nothing.

    Without the lock on the secret rows, the replacement would rotate the revoked row, which no
    deploy reads, and report success.
    """
    tag = uuid.uuid4().hex[:8]
    env, repo = f"env-{tag}", f"repo-{tag}"
    with Session(engine) as session:
        session.add(Environment(id=uuid.uuid4(), name=env, storage_root=f"file:///srv/{tag}"))
        session.commit()
        admin = _principal(session, f"admin-{tag}")
        RepositoryService(session, AllowAllAuthorization()).create(
            admin,
            name=repo,
            kind=RepositoryKind.SSH,
            url="git@git.example.com:acme/a.git",
            ssh_known_hosts=(
                "git.example.com ssh-ed25519 "
                "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR\n"
            ),
        )
        DeployKeyService(session, AllowAllAuthorization(), _KEYS).generate(admin, repo)
        secret = session.execute(
            select(Secret).where(Secret.name == f"{repo}-deploy-key")
        ).scalar_one()
        secret_id, ciphertext = secret.id, secret.ciphertext

    def _revoke(session: Session) -> None:
        with patch.object(session, "commit"):
            SecretService(session, AllowAllAuthorization(), _KEYS).revoke(admin, secret_id)
        session.flush()  # takes the row lock the replacement waits for

    def _replace(session: Session) -> None:
        DeployKeyService(session, AllowAllAuthorization(), _KEYS).generate(
            admin, repo, environments=[env], replace=True
        )

    outcome = _race(engine, _revoke, _replace)

    assert isinstance(outcome, ConflictError), outcome
    assert "revoked" in str(outcome)
    with Session(engine) as session:
        secret = session.get(Secret, secret_id)
        assert secret is not None
        assert secret.revoked_at is not None and secret.ciphertext == ciphertext
