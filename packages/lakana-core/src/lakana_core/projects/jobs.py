"""The checks of a project's jobs that need more than the file: names and selections."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from lakana_core.errors import ValidationError
from lakana_core.identifiers import refuse_outer_whitespace
from lakana_core.projects.dag import ProjectGraph
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig
from lakana_core.projects.yaml_loader import key_line
from lakana_core.runs.selection import ManifestGraph, Selection, resolve_selection


def job_issues(
    config: ResolvedProjectConfig,
    assets: Sequence[ParsedAsset],
    graph: ProjectGraph,
    *,
    config_text: str,
    environment: str,
    deployed_jobs: Collection[str] = (),
    path: str = "lakana.yaml",
) -> list[ProjectIssue]:
    """Check every job: a new name has no outer space, and each selects at least one asset.

    A job name that starts or ends with a space is refused unless the environment deployed a
    job of exactly that name before: an existing project must keep deploying, and renaming the
    job in git ends the exception. The sentence is the one of `refuse_outer_whitespace`.

    Parameters
    ----------
    config
        `lakana.yaml` resolved for the target environment.
    assets
        The project's assets.
    graph
        Their DAG.
    config_text
        The content of `lakana.yaml`, to point an issue at the line of the selection.
    environment
        The target environment; an issue points at its override of the selection if it has one.
    deployed_jobs
        The names of the jobs in the manifest of the last deployed project version of the
        environment; none before the first deploy.
    path
        The file's repository-relative path, for issues.

    Returns
    -------
    list of ProjectIssue
        One per new job name with an outer space, and one per job whose selection matches no
        asset, naming the pattern and the next step.
    """
    if not config.jobs:
        return []
    issues: list[ProjectIssue] = []
    for name in sorted(config.jobs):
        if name in deployed_jobs:
            continue
        try:
            refuse_outer_whitespace(name, what="Job name")
        except ValidationError as exc:
            issues.append(ProjectIssue(path, key_line(config_text, ("jobs", name)), str(exc)))
    known = ManifestGraph(assets=tuple(sorted(a.table for a in assets)), edges=graph.edges)
    for name, job in sorted(config.jobs.items()):
        try:
            resolve_selection(
                known,
                Selection(
                    patterns=tuple(job.select), upstream=job.upstream, downstream=job.downstream
                ),
            )
        except ValidationError as exc:
            line = key_line(config_text, ("environments", environment, "jobs", name, "select"))
            if line is None:
                line = key_line(config_text, ("jobs", name, "select"))
            reason = str(exc).replace("--select ", "select ", 1)
            issues.append(
                ProjectIssue(
                    path,
                    line,
                    f"job {name!r}: {reason}. Patterns are shell globs over table names such as "
                    "'gold.*'; change `select:` or add the asset.",
                )
            )
    return issues
