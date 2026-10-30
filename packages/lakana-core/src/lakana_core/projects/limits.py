"""Resource limits for one deploy."""

from __future__ import annotations

from dataclasses import dataclass

from lakana_core.config import DEFAULT_MAX_STATEMENT_CHARS


@dataclass(frozen=True)
class DeployLimits:
    """Bounds on what one deploy may consume.

    Parameters
    ----------
    git_command_seconds
        Longest a single git subprocess may run.
    job_seconds
        Wall-clock limit for the whole deploy job, fetch to write.
    checkout_bytes
        Largest `git archive` of the commit, and largest summed size of
        the extracted files.
    checkout_members
        Most entries (files, directories, links) extracted from the archive.
    sql_file_bytes
        Largest single asset file, SQL or Python.
    max_assets
        Most asset files under `assets/`.
    yaml_bytes
        Largest `lakana.yaml`, and largest single front-matter block.
    fetch_bytes
        Largest on-disk size of the temporary repository during a fetch.
    fetch_refs
        Most branches and tags a repository may have for a SHA deploy, counted
        with `ls-remote`.
    pyproject_bytes
        Largest `pyproject.toml` of a project with Python assets.
    lock_bytes
        Largest `uv.lock`.
    statement_chars
        Longest SQL of one SQL asset, in characters (`QueryLimitsConfig.max_statement_chars`).
    max_checks_per_table
        Most checks of one table under `checks/`.
    translation_bytes
        Largest translation file, one per language.
    translation_total_bytes
        Most bytes of all translation files together.
    """

    git_command_seconds: float = 120.0
    job_seconds: float = 600.0
    checkout_bytes: int = 200 * 1024 * 1024
    checkout_members: int = 100_000
    sql_file_bytes: int = 1024 * 1024
    max_assets: int = 2000
    yaml_bytes: int = 64 * 1024
    fetch_bytes: int = 1024 * 1024 * 1024
    fetch_refs: int = 10_000
    pyproject_bytes: int = 256 * 1024
    lock_bytes: int = 16 * 1024 * 1024
    statement_chars: int = DEFAULT_MAX_STATEMENT_CHARS
    max_checks_per_table: int = 50
    translation_bytes: int = 1024 * 1024
    translation_total_bytes: int = 10 * 1024 * 1024
