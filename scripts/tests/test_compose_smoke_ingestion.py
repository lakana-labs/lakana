"""Tests of the Compose smoke test's ingestion part, M6's exit criterion.

The script runs only in CI's ``compose-smoke`` job (never ``docker compose up`` locally), so these
tests pin what it must do: the sources outside the stack, the schedule, the bounded waits, the
assertions and the clean-up.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "compose_smoke_test.sh"


def _script() -> str:
    """Read the smoke test.

    Returns
    -------
    str
        Its text.
    """
    return SCRIPT.read_text()


def _ingestion() -> str:
    """Return the ingestion part: from where it starts to the end.

    Returns
    -------
    str
        The text after the marker line.
    """
    script = _script()
    marker = "# M6's exit criterion"
    assert script.count(marker) == 1
    return script.split(marker, 1)[1]


def test_the_smoke_test_is_valid_bash() -> None:
    """A syntax error would only show in the CI job."""
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)


def test_the_ingestion_runs_last_on_the_specialised_workers() -> None:
    """It follows the worker-kinds part, so the tasks run on `worker-pipeline`."""
    script = _script()
    assert script.index("set_env LAKANA_WORKERS 0") < script.index("# M6's exit criterion")
    assert script.rstrip().endswith('echo "compose smoke test: OK"')


def test_the_sources_listen_on_the_host_s_primary_address_outside_the_stack() -> None:
    """Neither an attached network nor a Docker gateway: egress and relay reach it as the LAN.

    Both share the host's network and listen on that address only; the script refuses to go on
    when `LAKANA_EGRESS_REFUSED` covers it.
    """
    part = _ingestion()
    assert "ip -4 route get 1.1.1.1" in part
    assert part.count("--network host") == 2
    assert '-c "listen_addresses=$0"' in part and 'PGPORT="$db_port"' in part
    assert '/api/stand_in.py /api "$host_ip" 0' in part
    assert "LAKANA_EGRESS_REFUSED" in part and "ip_network" in part
    assert '--host "$host_ip"' in part
    assert '--url "https://$host_ip:$api_port/api"' in part


def test_the_database_image_is_pinned_by_digest_and_pulled_first() -> None:
    """The same PostgreSQL 18 image as the Docker egress test, pulled before it is created."""
    tests = ROOT / "packages" / "lakana-worker" / "tests"
    pinned = re.search(
        r'^_POSTGRES = "(postgres:[^"]+)"',
        (tests / "test_docker_task_egress_integration.py").read_text(),
        re.M,
    )
    assert pinned and "@sha256:" in pinned.group(1)
    part = _ingestion()
    assert f'postgres_image="{pinned.group(1)}"' in part
    assert part.index('docker pull -q "$postgres_image"') < part.index("docker create")


def test_the_rest_source_runs_from_the_task_image_with_tls_1_2() -> None:
    """The example's stand-in, with a leaf for the host's address from a CA made per run."""
    part = _ingestion()
    assert '"$TASK_IMAGE-py$python" /api/stand_in.py' in part
    assert 'make_certs.py" "$sources/tls" "$host_ip"' in part
    stand_in = (ROOT / "docs" / "examples" / "ingest-rest" / "stand_in.py").read_text()
    assert "minimum_version = ssl.TLSVersion.TLSv1_2" in stand_in


def test_the_bundle_mounts_the_file_source_as_an_ingestion_root() -> None:
    """`LAKANA_INGESTION_ROOTS` is set in the bundle's `.env` and the launcher restarted."""
    part = _ingestion()
    root = part.index('setting = "LAKANA_INGESTION_ROOTS"')
    assert '{"exports": sys.argv[1]}' in part
    assert root < part.index("docker compose up -d --wait", root)
    assert "--kind directory --root exports --path orders" in part


def test_the_project_is_made_from_the_three_templates() -> None:
    """The assets `lakana init --template` writes, copied into one project, deployed unchanged."""
    part = _ingestion()
    assert 'lakana init "$ingest" --template ingest-sql' in part
    assert "for template in ingest-files ingest-rest; do" in part
    assert 'cp "$work/$template"/assets/bronze/*.py "$ingest/assets/bronze/"' in part
    assert '@asset(connections=["source-api"], retries=0)' in part


def test_the_runs_come_from_a_schedule_with_a_bound_principal() -> None:
    """A job every minute and a run principal; each run waited for at most four minutes."""
    part = _ingestion()
    assert 'cron: "* * * * *"' in part
    assert "lakana deployments set-run-as ingest --env dev --principal ingest-runner" in part
    deadlines = [int(n) for n in re.findall(r"deadline=\$\(\(SECONDS \+ (\d+)\)\)", part)]
    assert deadlines and max(deadlines) <= 240
    assert part.count("wait_for_scheduled_run ") == 2
    assert "lakana job pause ingest --env dev --repo ingest" in part
    assert "lakana job resume ingest --env dev --repo ingest" in part


def test_the_second_run_adds_exactly_the_new_rows() -> None:
    """Three rows per table first; then two files rows, two orders, one item updated, one added."""
    part = _ingestion()
    assert '{"files": 3, "orders": 3, "items": 3}' in part
    assert '{"files": 5, "orders": 5, "items": 4}' in part
    assert '"Buoy, large"' in part


def test_the_second_run_proves_each_read_is_incremental() -> None:
    """Run 2 is checked by what each task wrote and by source rows outside each read's window.

    A full re-read would give the same 5/5/4, because merges on `id` hide it.

    - files (`append`): `rows_written` 2, the new file only.
    - SQL (`merge`, `LOOK_BACK = 1000`): the seed ids are 1, 2 and 2004, then 2003 commits late
      and 2005 arrives, and order 1 changes at the source. Run 2 reads ids past 2004 - 1000:
      `rows_written` 2 (2003 and 2005 inserted, 2004 unchanged), and order 1 keeps its first
      amount. A full re-read would write 3 and change it.
    - REST (`merge`, one hour back): item 2 changes, item 4 is added, and item 1 is renamed at the
      source with its old `updated_at`, before the window. `rows_written` 2 and item 1 keeps its
      name; a full re-read would write 3 and rename it.

    A merge's `rows_written` counts inserted and changed rows (`MergeCounts`), so an unchanged row
    the window reads again does not show; the source changes outside the window do.
    """
    part = _ingestion()
    run_two = part.split('wait_for_scheduled_run "$first"', 1)[1]
    assert 'rows_written \'{"bronze.exports": 2, "bronze.orders": 2, "bronze.items": 2}\'' in (
        run_two
    )
    assert "(1, 'Ada', 12.50), (2, 'Grace', 8.00), (2004, 'Ada', 5.25)" in part
    assert "UPDATE orders SET amount = 99.00 WHERE id = 1;" in part
    assert "(2003, 'Linus', 20.00), (2005, 'Barbara', 9.75)" in part
    assert "[(1, 1), (2, 1), (2003, 1), (2004, 1), (2005, 1)]" in run_two
    assert "SELECT amount = 12.50 AS kept FROM bronze.orders WHERE id = 1" in run_two
    assert '"Anchor, renamed at the source"' in part
    assert '[(1, "Anchor"), (2, "Buoy, large"), (3, "Cleat"), (4, "Deck mop")]' in run_two


def test_the_exit_criterion_checks_lineage_egress_and_load() -> None:
    """Lineage to the PostgreSQL connection, the proxy's refusal of an undeclared host, a load."""
    part = _ingestion()
    assert "connection source-db (postgresql)" in part
    assert "is not a destination this task may reach" in part
    assert "lakana load" in part and "bronze.first_file" in part


def test_the_secrets_are_generated_at_run_time_and_read_from_stdin() -> None:
    """No literal secret; the connections take theirs through `--password-stdin`."""
    part = _ingestion()
    assert part.count("--password-stdin") == 2
    assert "secrets.token_urlsafe" in part


def test_the_cleanup_removes_the_sources_before_the_work_directory() -> None:
    """The source containers carry a label of this run and go with the bundle."""
    cleanup = _script().split("cleanup() {", 1)[1].split("\n}\n", 1)[0]
    removal = 'docker ps -aq --filter "label=lakana.smoke.source=$project_name"'
    assert removal in cleanup
    assert cleanup.index(removal) < cleanup.index('rm -rf "$work"')
    assert _ingestion().count('--label "lakana.smoke.source=$project_name"') == 2


def test_every_job_command_names_the_repository() -> None:
    """The script runs in the bundle's directory, which has no `lakana.yaml` to name the repo.

    `lakana job pause`, `resume` and `show` fall back to `lakana.yaml`'s `repo:` and stop with
    "Name a repository" without it; the first CI run failed that way.
    """
    lines = [line for line in _ingestion().splitlines() if "lakana job " in line]
    assert lines
    for line in lines:
        assert "--repo ingest" in line, line
