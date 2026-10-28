"""`LAKANA_LAUNCHER__*`: what the launcher needs, and the message for each mistake."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

import pytest

from lakana_worker.launcher import egress
from lakana_worker.launcher.settings import (
    KNOWN_SETTINGS,
    LauncherSettings,
    SettingsError,
    check_ingestion_roots,
    load_settings,
)

_IMAGES = '{"3.13": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"}'


def test_the_two_required_settings_and_the_defaults() -> None:
    """Only the stack and the images must be set; everything else has a working default."""
    settings = load_settings(
        {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    )

    assert settings == LauncherSettings(
        stack="lakana",
        images={"3.13": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"},
        work_volume="lakana-task-work",
    )
    assert settings.socket == Path("/run/lakana-launcher/launcher.sock")
    assert settings.no_new_privileges == "auto"
    assert (settings.max_memory_mb, settings.max_cpus, settings.max_pids) == (16384, 8.0, 4096)


@pytest.mark.parametrize(
    "name",
    [
        "TASK_USER",
        "BUILD_USER",
        "DOCKER_SOCKET",
        "STATE_DIR",
        "EGRESS_KEY",
        "SERVER_SERVICE",
        "SERVER_ALIAS",
        "EGRESS_SERVICE",
        "MAX_SECONDS",
        "KEEP_ENVIRONMENTS",
        "WORK_VOLUME",
    ],
)
def test_only_what_a_person_needs_is_a_setting(name: str) -> None:
    """Users, service names, paths of the stack and the like are constants, not variables.

    Parameters
    ----------
    name
        A former variable, after the prefix.
    """
    environ = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    environ["LAKANA_LAUNCHER__" + name] = "0:0"

    with pytest.raises(SettingsError, match=f"{name} is not a setting .it is a constant now"):
        load_settings(environ)

    settings = load_settings(
        {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    )
    assert settings.task_user == "10001:10003" and settings.build_user == "10002:10002"
    assert settings.docker_socket == Path("/var/run/docker.sock")


def test_the_task_limits_are_settings() -> None:
    """The ceilings of memory, CPUs and processes are the three that a person tunes."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            "LAKANA_LAUNCHER__MAX_MEMORY_MB": "8192",
            "LAKANA_LAUNCHER__MAX_CPUS": "4",
            "LAKANA_LAUNCHER__MAX_PIDS": "512",
        }
    )

    assert (settings.max_memory_mb, settings.max_cpus, settings.max_pids) == (8192, 4.0, 512)


def test_the_probe_cap_is_a_known_setting_with_a_default() -> None:
    """A root with many files needs a higher cap, so it is a variable and a documented name."""
    base = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}

    assert "PROBE_MAX_ENTRIES" in KNOWN_SETTINGS
    assert load_settings(base).probe_max_entries == 100000
    assert (
        load_settings({**base, "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES": "5000000"}).probe_max_entries
        == 5000000
    )


def test_the_network_range_and_the_total_memory_have_defaults_that_work() -> None:
    """Task networks come from 10.207.0.0/16 unless told otherwise; memory is bounded in total."""
    settings = load_settings(
        {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    )

    assert settings.network_range == "10.207.0.0/16"
    assert settings.max_total_memory_mb == 65536
    assert "NETWORK_RANGE" in KNOWN_SETTINGS and "MAX_TOTAL_MEMORY_MB" in KNOWN_SETTINGS


def test_the_network_range_and_the_total_memory_can_be_set() -> None:
    """A smaller or another private range, and a bigger total."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            "LAKANA_LAUNCHER__NETWORK_RANGE": "192.168.64.0/24",
            "LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB": "131072",
        }
    )

    assert settings.network_range == "192.168.64.0/24" and settings.max_total_memory_mb == 131072


_REFUSED = "LAKANA_LAUNCHER__EGRESS_REFUSED"
_REFUSED_FORMAT = (
    "list CIDR ranges or addresses separated by commas, such as 192.168.1.0/24,10.0.0.7."
)


def test_the_refused_ranges_are_read_and_an_address_is_one_host() -> None:
    """Blank entries are ignored; an address means /32 or /128; IPv6 works too."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            _REFUSED: "192.168.1.0/24, 10.0.0.7,,fd00::/8",
        }
    )

    assert settings.egress_refused == ("192.168.1.0/24", "10.0.0.7/32", "fd00::/8")
    assert "EGRESS_REFUSED" in KNOWN_SETTINGS


@pytest.mark.parametrize("value", [None, "", "  ", " , ,"])
def test_no_refused_ranges_by_default(value: str | None) -> None:
    """Unset, blank or only commas: nothing beyond the built-in refusals.

    Parameters
    ----------
    value
        The variable's value, or `None` for unset.
    """
    environ = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    if value is not None:
        environ[_REFUSED] = value

    assert load_settings(environ).egress_refused == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "192.168.1.5/24",
            f"{_REFUSED}: `192.168.1.5/24` is not a network (host bits set; did you mean "
            f"192.168.1.0/24?); {_REFUSED_FORMAT}",
        ),
        (
            "10.0.0.0/8,lan",
            f"{_REFUSED}: `lan` is not an IP address or CIDR range; {_REFUSED_FORMAT}",
        ),
        (
            "10.0.0.0/33",
            f"{_REFUSED}: `10.0.0.0/33` is not an IP address or CIDR range; {_REFUSED_FORMAT}",
        ),
        pytest.param(
            ",".join(f"10.0.{n}.0/24" for n in range(65)),
            f"{_REFUSED} lists 65 ranges, more than 64; list wider ranges instead, such as "
            "192.168.0.0/16.",
            id="65-ranges",
        ),
    ],
)
def test_a_malformed_refused_range_stops_the_launcher(value: str, message: str) -> None:
    """Host bits set, not an address, or too many: the message names the entry and the format.

    Parameters
    ----------
    value
        The variable's value.
    message
        The whole error.
    """
    with pytest.raises(SettingsError) as caught:
        load_settings({**_BASE, _REFUSED: value})

    assert str(caught.value) == message


def test_an_ipv4_mapped_entry_is_read_as_its_ipv4_range() -> None:
    """`::ffff:192.168.1.10` would never match (the checks map such addresses to IPv4 first)."""
    settings = load_settings(
        {**_BASE, _REFUSED: "::ffff:192.168.1.10, ::ffff:10.1.0.0/112, 192.168.2.0/24"}
    )

    assert settings.egress_refused == ("192.168.1.10/32", "10.1.0.0/16", "192.168.2.0/24")
    refused = frozenset(ipaddress.ip_network(r) for r in settings.egress_refused)
    for address in ("192.168.1.10", "::ffff:192.168.1.10", "10.1.200.3", "::ffff:10.1.200.3"):
        assert egress.forbidden(ipaddress.ip_address(address), refused, frozenset()), address


def test_sixty_four_refused_ranges_are_allowed() -> None:
    """The cap is 64, inclusive."""
    value = ",".join(f"10.0.{n}.0/24" for n in range(64))

    assert len(load_settings({**_BASE, _REFUSED: value}).egress_refused) == 64


def test_an_allowed_ingestion_root_is_kept() -> None:
    """A plain host directory passes."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            "LAKANA_LAUNCHER__INGESTION_ROOTS": '{"landing": "/srv/landing", "x": "/data/x"}',
        }
    )

    assert settings.ingestion_roots == {"landing": "/srv/landing", "x": "/data/x"}


_BASE = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
_ROOTS = "LAKANA_LAUNCHER__INGESTION_ROOTS"
_SECRET_PATH = "srv/secret-landing"


def _roots(path: str) -> dict[str, str]:
    """Build an environment whose only ingestion root is `path`.

    Parameters
    ----------
    path
        The host directory.

    Returns
    -------
    dict
        The launcher's environment.
    """
    return {**_BASE, _ROOTS: json.dumps({"landing": path})}


@pytest.mark.parametrize("value", [None, "", "{}"])
def test_no_ingestion_roots_from_what_the_bundle_passes_by_default(value: str | None) -> None:
    """Unset, blank (a bundle `.env` without `LAKANA_INGESTION_ROOTS`) or `{}`: no roots.

    Parameters
    ----------
    value
        The variable's value, or `None` for unset.
    """
    environ = dict(_BASE)
    if value is not None:
        environ[_ROOTS] = value

    assert load_settings(environ).ingestion_roots == {}


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        pytest.param(
            {"LAKANA_LAUNCHER__IMAGES": _IMAGES}, "LAKANA_LAUNCHER__STACK is not set", id="no-stack"
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__STACK": "Lakana"},
            "LAKANA_LAUNCHER__STACK must be",
            id="stack-upper",
        ),
        pytest.param(
            {"LAKANA_LAUNCHER__STACK": "lakana"},
            "LAKANA_LAUNCHER__IMAGES is not set",
            id="no-images",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.10": "x"}'},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="python-3-10",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.13": "a b"}'},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="image-space",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": "[1]"},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="images-list",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NO_NEW_PRIVILEGES": "off"},
            "must be auto or on",
            id="nnp-off",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_PIDS": "lots"},
            "LAKANA_LAUNCHER__MAX_PIDS must be",
            id="pids-text",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_PIDS": "\u00b2"},
            "LAKANA_LAUNCHER__MAX_PIDS must be",
            id="pids-superscript",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES": "0"},
            "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES must be",
            id="probe-cap-zero",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NETWORK_RANGE": "8.8.0.0/16"},
            "LAKANA_LAUNCHER__NETWORK_RANGE must be a private IPv4 range",
            id="range-public",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NETWORK_RANGE": "10.207.0.0/28"},
            "LAKANA_LAUNCHER__NETWORK_RANGE must be a private IPv4 range",
            id="range-too-small",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NETWORK_RANGE": "fd00::/48"},
            "LAKANA_LAUNCHER__NETWORK_RANGE must be a private IPv4 range",
            id="range-ipv6",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NETWORK_RANGE": "10.207.0.5/16"},
            "LAKANA_LAUNCHER__NETWORK_RANGE must be a private IPv4 range",
            id="range-host-bits",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NETWORK_RANGE": "10.0.0.0/7"},
            "LAKANA_LAUNCHER__NETWORK_RANGE must be a private IPv4 range",
            id="range-wider-than-private",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB": "1024"},
            "LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB must be at least",
            id="total-below-single",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_CPUS": "0"},
            "LAKANA_LAUNCHER__MAX_CPUS must be",
            id="cpus-zero",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_MEMORY_MB": "1"},
            "LAKANA_LAUNCHER__MAX_MEMORY_MB must be",
            id="memory-tiny",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "launcher.sock"},
            "LAKANA_LAUNCHER__SOCKET must be an absolute path",
            id="socket-relative",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "/run/../x.sock"},
            "LAKANA_LAUNCHER__SOCKET must be an absolute path",
            id="socket-dots",
        ),
        pytest.param({**_BASE, _ROOTS: "[1]"}, "INGESTION_ROOTS must be", id="roots-list"),
        pytest.param({**_BASE, _ROOTS: "{nope"}, "INGESTION_ROOTS must be", id="roots-bad-json"),
        pytest.param(
            {**_BASE, _ROOTS: '{"Bad Name": "/srv/x"}'}, "INGESTION_ROOTS must be", id="root-name"
        ),
        pytest.param({**_BASE, _ROOTS: '{"x": 1}'}, "INGESTION_ROOTS must be", id="root-not-text"),
        pytest.param(_roots("srv/x"), "INGESTION_ROOTS must be", id="root-relative"),
        pytest.param(_roots("/srv/../etc"), "INGESTION_ROOTS must be", id="root-dots"),
        pytest.param(_roots("/srv/x/"), "INGESTION_ROOTS must be", id="root-trailing-slash"),
        pytest.param(_roots("/"), "INGESTION_ROOTS must be", id="root-slash"),
        pytest.param(_roots("/run"), "INGESTION_ROOTS must be", id="root-run"),
        pytest.param(
            _roots("/run/lakana-launcher"), "INGESTION_ROOTS must be", id="root-under-run"
        ),
        pytest.param(_roots("/var"), "INGESTION_ROOTS must be", id="root-var-holds-docker-socket"),
        pytest.param(_roots("/var/run"), "INGESTION_ROOTS must be", id="root-var-run"),
        pytest.param(_roots("/var/run/docker"), "INGESTION_ROOTS must be", id="root-under-var-run"),
        pytest.param(_roots("/var/lib"), "INGESTION_ROOTS must be", id="root-var-lib-holds-docker"),
        pytest.param(
            _roots("/var/lib/docker"), "INGESTION_ROOTS must be", id="root-var-lib-docker"
        ),
        pytest.param(
            _roots("/var/lib/docker/volumes"), "INGESTION_ROOTS must be", id="root-under-docker"
        ),
        pytest.param(_roots("/proc"), "INGESTION_ROOTS must be", id="root-proc"),
        pytest.param(_roots("/proc/1/root"), "INGESTION_ROOTS must be", id="root-under-proc"),
        pytest.param(_roots("/sys"), "INGESTION_ROOTS must be", id="root-sys"),
        pytest.param(_roots("/sys/fs"), "INGESTION_ROOTS must be", id="root-under-sys"),
        pytest.param(_roots("/dev"), "INGESTION_ROOTS must be", id="root-dev"),
        pytest.param(_roots("/dev/shm"), "INGESTION_ROOTS must be", id="root-under-dev"),
        pytest.param(_roots("/etc"), "INGESTION_ROOTS must be", id="root-etc"),
        pytest.param(_roots("/etc/ssl"), "INGESTION_ROOTS must be", id="root-under-etc"),
        pytest.param(_roots("//"), "INGESTION_ROOTS must be", id="root-double-slash"),
        pytest.param(_roots("/srv/a\nb"), "INGESTION_ROOTS must be", id="root-newline"),
        pytest.param(_roots("/srv/a\x00b"), "INGESTION_ROOTS must be", id="root-nul"),
        pytest.param(_roots("/srv/a\x1bb"), "INGESTION_ROOTS must be", id="root-escape"),
        pytest.param(_roots("/srv/a\x7fb"), "INGESTION_ROOTS must be", id="root-delete"),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "/run/a\nb.sock"},
            "SOCKET must be an absolute path",
            id="socket-newline",
        ),
        pytest.param(_roots("//run"), "INGESTION_ROOTS must be", id="root-double-slash-run"),
        pytest.param(
            _roots("//var/run"), "INGESTION_ROOTS must be", id="root-double-slash-var-run"
        ),
        pytest.param(
            _roots("//var/lib/docker/volumes"),
            "INGESTION_ROOTS must be",
            id="root-double-slash-docker",
        ),
        pytest.param(
            _roots("//srv/landing"), "INGESTION_ROOTS must be", id="root-double-slash-plain"
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "//run/x.sock"},
            "SOCKET must be an absolute path",
            id="socket-double-slash",
        ),
        pytest.param(
            _roots("/var/lib/containerd"), "INGESTION_ROOTS must be", id="root-containerd"
        ),
        pytest.param(_roots("/var/snap/docker"), "INGESTION_ROOTS must be", id="root-snap-docker"),
        pytest.param(_roots("/var/snap"), "INGESTION_ROOTS must be", id="root-holds-snap-docker"),
        pytest.param(_roots("/root"), "INGESTION_ROOTS must be", id="root-root-home"),
        pytest.param(_roots("/root/data"), "INGESTION_ROOTS must be", id="root-under-root-home"),
        pytest.param(_roots("/boot"), "INGESTION_ROOTS must be", id="root-boot"),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.13": "a:1", "3.13": "b:2"}'},
            "repeats",
            id="images-repeated-key",
        ),
        pytest.param(
            {**_BASE, _ROOTS: '{"a": "/srv/a", "a": "/srv/b"}'},
            "INGESTION_ROOTS must be",
            id="roots-repeated-key",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_MEMORY": "1"},
            "LAKANA_LAUNCHER__MAX_MEMORY is not a setting; the settings are",
            id="unknown-variable",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__TASK_USER": "0:0"},
            "LAKANA_LAUNCHER__TASK_USER is not a setting",
            id="former-variable",
        ),
        pytest.param(
            {**_roots("/srv"), "LAKANA_LAUNCHER__SOCKET": "/srv/launcher/launcher.sock"},
            "INGESTION_ROOTS must be",
            id="root-holds-the-launcher-socket",
        ),
        pytest.param(
            {**_roots("/srv/launcher"), "LAKANA_LAUNCHER__SOCKET": "/srv/launcher/launcher.sock"},
            "INGESTION_ROOTS must be",
            id="root-is-the-launcher-socket-directory",
        ),
    ],
)
def test_each_mistake_names_the_setting_and_never_echoes_a_value(
    environ: dict[str, str], message: str
) -> None:
    """The ingestion roots are host paths; a message names the setting, not the value.

    Parameters
    ----------
    environ
        The launcher's environment.
    message
        Part of the error.
    """
    with pytest.raises(SettingsError, match=message) as caught:
        load_settings(environ)
    for name, value in environ.items():
        if name == _ROOTS:
            assert value not in str(caught.value)
    assert _SECRET_PATH not in str(caught.value)


def test_a_refused_root_does_not_name_its_path() -> None:
    """The path an operator wrote stays out of the log."""
    with pytest.raises(SettingsError) as caught:
        load_settings(_roots("/etc/" + _SECRET_PATH))
    assert _SECRET_PATH not in str(caught.value)


def test_a_blank_former_variable_is_ignored_and_a_set_one_says_to_remove_it() -> None:
    """Compose passes empty values for unset variables; only a value is a mistake."""
    blank = {**_BASE, "LAKANA_LAUNCHER__TASK_USER": "  "}

    assert load_settings(blank) == load_settings(_BASE)
    with pytest.raises(SettingsError, match=r"TASK_USER is not a setting .*remove it"):
        load_settings({**_BASE, "LAKANA_LAUNCHER__TASK_USER": "0:0"})


def test_the_relays_reachable_networks_are_a_test_hook_and_never_loaded() -> None:
    """`relay_reachable` lets the Docker tests' relay dial a network it is attached to.

    The environment cannot set it: it is empty from `load_settings`, and a variable of that
    name stops the launcher like any other unknown setting.
    """
    assert load_settings(_BASE).relay_reachable == ()
    assert "RELAY_REACHABLE" not in KNOWN_SETTINGS
    with pytest.raises(SettingsError, match=r"LAKANA_LAUNCHER__RELAY_REACHABLE is not a setting"):
        load_settings({**_BASE, "LAKANA_LAUNCHER__RELAY_REACHABLE": "172.30.0.0/16"})


_REPO = Path(__file__).resolve().parents[3]
"""The repository root: ``packages/lakana-worker/tests`` is three levels below it."""
_COMPOSE = _REPO / "packages" / "lakana" / "src" / "lakana" / "cli" / "deploy" / "compose.yaml"


def test_the_repository_root_holds_the_bundle_template() -> None:
    """A moved test or template fails here, by name, not as a confusing read error."""
    assert (_REPO / "pyproject.toml").is_file() and (_REPO / "Dockerfile").is_file(), (
        f"{_REPO} is not the repository root; fix _REPO in {Path(__file__).name}"
    )
    assert _COMPOSE.is_file(), (
        f"the Compose bundle's template is not at {_COMPOSE.relative_to(_REPO)}; point "
        f"_COMPOSE in {Path(__file__).name} at lakana's compose.yaml"
    )


_VARIABLE = re.compile(r"\$\{(\w+)(?::([-?])([^}]*))?\}")


def _bundle_environment(service: str, values: dict[str, str]) -> dict[str, str]:
    """Read a service's environment from the Compose bundle, interpolated as Compose would.

    Parameters
    ----------
    service
        The service.
    values
        The `.env` values and `COMPOSE_PROJECT_NAME`; a missing one takes its default.

    Returns
    -------
    dict
        Variable to value.
    """
    yaml = pytest.importorskip("yaml")
    if not _COMPOSE.is_file():
        pytest.fail(f"no Compose template at {_COMPOSE}: see the test of the repository root")
    environment = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"][service][
        "environment"
    ]

    def value(match: re.Match[str]) -> str:
        """Resolve one reference.

        Parameters
        ----------
        match
            ``${NAME}``, ``${NAME:-default}`` or ``${NAME:?message}``.

        Returns
        -------
        str
            The value.
        """
        name, kind, rest = match.groups()
        if values.get(name):
            return values[name]
        assert kind == "-", f"{name} is required by the bundle"
        return rest

    return {key: _VARIABLE.sub(value, str(text)) for key, text in environment.items()}


def test_the_bundle_gives_the_launcher_and_the_proxy_settings_they_accept() -> None:
    """Every variable the bundle sets is a setting, and its value passes the check."""
    values = {
        "COMPOSE_PROJECT_NAME": "lakana",
        "LAKANA_TASK_IMAGE": "ghcr.io/lakana-labs/lakana-task:0.20.0",
        "LAKANA_EGRESS_UPSTREAM_PROXY": "",
    }

    settings = load_settings(_bundle_environment("launcher", values))
    refusing = load_settings(
        _bundle_environment("launcher", {**values, "LAKANA_EGRESS_REFUSED": "192.168.1.0/24"})
    )
    assert settings.egress_refused == ()
    assert refusing.egress_refused == ("192.168.1.0/24",)
    proxy = egress.load_settings(_bundle_environment("egress", values))

    assert settings.stack == "lakana"
    assert settings.work_volume == "lakana-task-work"
    assert settings.images == {
        version: f"ghcr.io/lakana-labs/lakana-task:0.20.0-py{version}"
        for version in ("3.11", "3.12", "3.13", "3.14")
    }
    assert proxy.upstream is None
    assert proxy.key == settings.egress_key


def test_the_ingestion_roots_check_is_the_launchers_and_names_the_variable() -> None:
    """The worker checks its own roots with the launcher's rule and the launcher's words."""
    launcher = check_ingestion_roots({"shared": "/etc"}, Path("/run/l.sock"))
    worker = check_ingestion_roots(
        {"shared": "/etc"}, Path("/run/l.sock"), variable="LAKANA_RUN__INGESTION_ROOTS"
    )
    with pytest.raises(SettingsError) as caught:
        load_settings(
            {
                "LAKANA_LAUNCHER__STACK": "lakana",
                "LAKANA_LAUNCHER__IMAGES": _IMAGES,
                "LAKANA_LAUNCHER__INGESTION_ROOTS": '{"shared": "/etc"}',
            }
        )

    assert launcher == str(caught.value)
    assert launcher is not None
    assert launcher.startswith("LAKANA_LAUNCHER__INGESTION_ROOTS must be a JSON object")
    assert worker == launcher.replace(
        "LAKANA_LAUNCHER__INGESTION_ROOTS", "LAKANA_RUN__INGESTION_ROOTS"
    )
    assert check_ingestion_roots({"ok": "/srv/in"}, Path("/run/l.sock")) is None
    assert check_ingestion_roots({"x": "/run/l"}, Path("/run/l/l.sock")) is not None
    assert check_ingestion_roots({"x": 1}, Path("/run/l.sock")) is not None
