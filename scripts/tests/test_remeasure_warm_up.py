"""The warm-up of the slow-test re-run: which test takes a candidate's one-off setup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import remeasure_warm_up as plugin
from scripts.remeasure_warm_up import WARM_UP_ENV, read_plan

pytest_plugins = ["pytester"]

SESSION_FIXTURE = """
import pytest

@pytest.fixture(scope="session")
def db():
    return "schema"

@pytest.fixture(scope="module")
def table(db):
    return "table"

@pytest.fixture
def row():
    return 1
"""


def _ran(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[str],
    seconds: dict[str, float],
    *args: str,
) -> list[str]:
    """Run the fixture project with the plugin and name the tests that ran, in order.

    Parameters
    ----------
    pytester
        The fixture project.
    monkeypatch
        Sets the plan's environment variable.
    candidates
        The candidates' node ids.
    seconds
        The shard's seconds by node id.
    *args
        More arguments for pytest.

    Returns
    -------
    list of str
        The node ids whose call ran or was skipped, in the order they ran.
    """
    plan = pytester.path / "plan.json"
    plan.write_text(json.dumps({"candidates": candidates, "seconds": seconds}))
    monkeypatch.setenv(WARM_UP_ENV, str(plan))
    reprec = pytester.inline_run("-p", "no:cacheprovider", *args, plugins=[plugin])
    return [
        report.nodeid
        for report in reprec.getreports("pytest_runtest_logreport")
        if report.when == "call" or (report.when == "setup" and report.skipped)
    ]


def test_a_candidate_is_warmed_up_by_a_test_that_shares_its_session_fixture(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest test using the candidate's session fixture, not a faster one without it."""
    pytester.makeconftest(SESSION_FIXTURE)
    pytester.makepyfile(
        test_m="""
        def test_pure(): pass
        def test_with_db(db, row): pass
        def test_with_db_slower(db): pass
        def test_candidate(db, row): pass
        """
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_candidate"],
        {
            "test_m.py::test_pure": 0.01,
            "test_m.py::test_with_db": 0.2,
            "test_m.py::test_with_db_slower": 0.4,
            "test_m.py::test_candidate": 2.6,
        },
    )
    assert ran == ["test_m.py::test_with_db", "test_m.py::test_candidate"]


def test_a_module_fixture_and_its_session_fixture_must_both_be_shared(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test with only the session fixture leaves the module's setup on the candidate."""
    pytester.makeconftest(SESSION_FIXTURE)
    pytester.makepyfile(
        test_m="""
        def test_with_db(db): pass
        def test_with_table(table): pass
        def test_candidate(table, row): pass
        """
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_candidate"],
        {"test_m.py::test_with_db": 0.01, "test_m.py::test_with_table": 0.3},
    )
    assert ran == ["test_m.py::test_with_table", "test_m.py::test_candidate"]


def test_a_warm_up_that_serves_two_fixture_sets_runs_once(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test that set the module up for one candidate is not run again for the next."""
    pytester.makeconftest(SESSION_FIXTURE)
    pytester.makepyfile(
        test_m="""
        def test_with_table(table): pass
        def test_needs_db(db): pass
        def test_needs_table(table): pass
        """
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_needs_db", "test_m.py::test_needs_table"],
        {"test_m.py::test_with_table": 0.3},
    )
    assert ran == [
        "test_m.py::test_with_table",
        "test_m.py::test_needs_db",
        "test_m.py::test_needs_table",
    ]


def test_a_doctest_candidate_shares_no_fixtures_and_takes_the_cheapest_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item without pytest fixtures needs no particular setup: any cheap test warms it."""
    pytester.makepyfile(
        test_m='''
        """
        >>> 1
        1
        """
        def test_quick(): pass
        '''
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_m"],
        {"test_m.py::test_quick": 0.01},
        "--doctest-modules",
    )
    assert ran == ["test_m.py::test_quick", "test_m.py::test_m"]


RECORDING_FIXTURES = """
import os
from pathlib import Path

import pytest


def _note(what):
    test = os.environ["PYTEST_CURRENT_TEST"].rsplit(" ", 1)[0]
    with Path(__file__).with_name("setups.txt").open("a") as log:
        log.write(f"{what} {test}\\n")


@pytest.fixture(scope="session")
def db():
    _note("db")
    return "schema"


@pytest.fixture(scope="module")
def table(db):
    _note("table")
    return "table"


@pytest.fixture
def row():
    _note("row")
    return 1
"""


def _setups(pytester: pytest.Pytester) -> list[str]:
    """Name each recorded fixture setup and the test whose setup ran it.

    Parameters
    ----------
    pytester
        The fixture project, whose fixtures wrote ``setups.txt``.

    Returns
    -------
    list of str
        ``"<fixture> <node id>"``, in the order they were set up.
    """
    return (pytester.path / "setups.txt").read_text().splitlines()


def test_a_module_without_a_sharing_test_sets_the_candidate_s_shared_fixtures_up_first(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test of the module shares the setup: a no-op item beside the candidate pays it.

    The OpenAPI document of the server's tests is built once per process by a session fixture
    (`_openapi_text`); a candidate alone in its module with it was timed with the build (2.6 s
    on its own on PR #739's run). Its own function-scoped fixtures stay on its clock.
    """
    pytester.makeconftest(RECORDING_FIXTURES)
    pytester.makepyfile(
        test_m="""
        def test_pure(): pass
        def test_candidate(table, row): pass
        """,
        test_other="""
        def test_with_db(db): pass
        """,
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_candidate"],
        {"test_m.py::test_pure": 0.01, "test_other.py::test_with_db": 0.01},
    )
    assert ran == ["test_m.py::test_candidate[warm-up]", "test_m.py::test_candidate"]
    assert _setups(pytester) == [
        "db test_m.py::test_candidate[warm-up]",
        "table test_m.py::test_candidate[warm-up]",
        "row test_m.py::test_candidate",
    ]


def test_the_fixture_warm_up_sets_up_the_candidate_s_parameter(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parametrized shared fixture is set up with the candidate's own parameter."""
    pytester.makeconftest(
        RECORDING_FIXTURES
        + """

@pytest.fixture(scope="module", params=["a", "b"])
def flavour(request):
    _note(f"flavour-{request.param}")
    return request.param
"""
    )
    pytester.makepyfile(
        test_m="""
        import pytest

        @pytest.mark.parametrize("n", [1, 2], scope="module")
        def test_candidate(flavour, n, row): pass
        """
    )
    ran = _ran(pytester, monkeypatch, ["test_m.py::test_candidate[b-2]"], {})
    assert ran == ["test_m.py::test_candidate[warm-up][b-2]", "test_m.py::test_candidate[b-2]"]
    assert _setups(pytester) == [
        "flavour-b test_m.py::test_candidate[warm-up][b-2]",
        "row test_m.py::test_candidate[b-2]",
    ]


def test_the_fixture_warm_up_of_a_method_runs_in_its_class(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class-scoped fixture of the candidate's class is shared setup too."""
    pytester.makeconftest(RECORDING_FIXTURES)
    pytester.makepyfile(
        test_m="""
        import pytest

        @pytest.fixture(scope="class")
        def shelf(db):
            return "shelf"

        class TestThings:
            def test_candidate(self, shelf, row): pass
        """
    )
    ran = _ran(pytester, monkeypatch, ["test_m.py::TestThings::test_candidate"], {})
    assert ran == [
        "test_m.py::TestThings::test_candidate[warm-up]",
        "test_m.py::TestThings::test_candidate",
    ]
    assert _setups(pytester) == [
        "db test_m.py::TestThings::test_candidate[warm-up]",
        "row test_m.py::TestThings::test_candidate",
    ]


def test_a_candidate_without_shared_fixtures_alone_in_its_module_runs_alone(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to set up first: no warm-up at all."""
    pytester.makeconftest(RECORDING_FIXTURES)
    pytester.makepyfile(test_m="def test_candidate(row): pass")
    ran = _ran(pytester, monkeypatch, ["test_m.py::test_candidate"], {})
    assert ran == ["test_m.py::test_candidate"]


def test_skipped_tests_are_never_warm_ups(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marked skipped, skipped in the shard, or not in the shard at all: none sets anything up.

    The candidate's shared fixtures are then set up by a no-op item of its own.
    """
    pytester.makeconftest(SESSION_FIXTURE)
    pytester.makepyfile(
        test_m="""
        import pytest

        @pytest.mark.skip(reason="off")
        def test_marked(db): pass
        def test_skipped_in_the_shard(db): pass
        def test_not_in_the_shard(db): pass
        def test_candidate(db): pass
        """
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_m.py::test_candidate"],
        {"test_m.py::test_marked": 0.0},
    )
    assert ran == ["test_m.py::test_candidate[warm-up]", "test_m.py::test_candidate"]


def test_candidates_sharing_a_warm_up_run_it_once_and_never_warm_each_other(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One warm-up per module and fixture set, before the first candidate it serves."""
    pytester.makeconftest(SESSION_FIXTURE)
    pytester.makepyfile(
        test_a="""
        def test_first(db): pass
        def test_quick(db): pass
        def test_second(db, row): pass
        """,
        test_b="""
        def test_third(db): pass
        def test_quick_too(db): pass
        """,
    )
    ran = _ran(
        pytester,
        monkeypatch,
        ["test_a.py::test_first", "test_a.py::test_second", "test_b.py::test_third"],
        {
            "test_a.py::test_first": 0.0,
            "test_a.py::test_quick": 0.1,
            "test_a.py::test_second": 0.0,
            "test_b.py::test_quick_too": 0.1,
        },
    )
    assert ran == [
        "test_a.py::test_quick",
        "test_a.py::test_first",
        "test_a.py::test_second",
        "test_b.py::test_quick_too",
        "test_b.py::test_third",
    ]


def test_without_a_plan_the_plugin_changes_nothing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded by accident, it runs every collected test."""
    monkeypatch.delenv(WARM_UP_ENV, raising=False)
    pytester.makepyfile(test_m="def test_a(): pass\ndef test_b(): pass")
    reprec = pytester.inline_run("-p", "no:cacheprovider", plugins=[plugin])
    assert reprec.countoutcomes() == [2, 0, 0]


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"candidates": "x", "seconds": {}}', '{"candidates": [], "seconds": []}'],
)
def test_a_broken_plan_is_refused_with_its_fix(tmp_path: Path, text: str) -> None:
    """The error names the file and who writes it."""
    plan = tmp_path / "plan.json"
    plan.write_text(text)
    with pytest.raises(pytest.UsageError, match=r"time_budgets\.py"):
        read_plan(plan)


def test_a_missing_plan_is_refused(tmp_path: Path) -> None:
    """The variable names a file that is not there."""
    with pytest.raises(pytest.UsageError, match="cannot read"):
        read_plan(tmp_path / "gone.json")
