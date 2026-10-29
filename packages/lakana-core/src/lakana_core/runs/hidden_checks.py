"""Which generated reference checks a reader is not shown.

A generated ``ref_<column>`` check reads the table its reference points at. Its result (that it
passed, its failing rows, its error) says something about that table, so a reader who may not
read the target is not shown the result at all, only a count of what was withheld, as
`TableModelView.hidden_references` counts the references themselves. Every other check, a
``key_*`` check and every hand-written one, is shown as before.

The target is the one the run's project version declared for the check
(`CheckSpec.generated.target`). The read right is asked of it by name and before anything is
looked up, as for a reference, so a missing target and an unreadable one are withheld alike. A
result whose target cannot be told (its project version holds no readable entry for it) is shown
only to a reader who may read every table of the environment.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import JobVersion, Run
from lakana_core.projects.checks import read_manifest_checks
from lakana_core.projects.model_checks import REF_PREFIX


class ReferenceCheckFilter:
    """Decide, for one reader in one environment, which check results to withhold.

    Manifests and read rights are looked up once per run and per target.

    Parameters
    ----------
    session
        Database session; read only.
    authz
        The authorization service.
    principal
        The reader.
    environment
        The environment's name.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        principal: PrincipalRef,
        environment: str,
    ) -> None:
        self._session = session
        self._authz = authz
        self._principal = principal
        self._environment = environment
        self._targets: dict[uuid.UUID, dict[str, str]] = {}
        self._readable: dict[str | None, bool] = {}

    def hides(self, run_id: uuid.UUID, name: str, generated: bool) -> bool:
        """Say whether a check result is withheld from the reader.

        Parameters
        ----------
        run_id
            The run the result belongs to.
        name
            The check's name, ``<table>/<check>``.
        generated
            Whether the check was generated from the asset's header.

        Returns
        -------
        bool
            `True` for a generated reference check whose target the reader may not read.
        """
        if not generated or not name.rpartition("/")[2].startswith(REF_PREFIX):
            return False
        target = self._targets_of(run_id).get(name)
        if target not in self._readable:
            self._readable[target] = self._may_read(target)
        return not self._readable[target]

    def _targets_of(self, run_id: uuid.UUID) -> dict[str, str]:
        """Map the generated reference checks of a run's project version to their targets.

        Parameters
        ----------
        run_id
            The run.

        Returns
        -------
        dict of str to str
            Check name to the target's dotted name; empty when the version is gone.
        """
        if run_id not in self._targets:
            manifest = self._session.execute(
                select(JobVersion.manifest_json)
                .join(Run, Run.job_version_id == JobVersion.id)
                .where(Run.id == run_id)
            ).scalar_one_or_none()
            self._targets[run_id] = {
                spec.name: spec.generated.target
                for spec in read_manifest_checks(manifest).specs
                if spec.generated is not None and spec.generated.target is not None
            }
        return self._targets[run_id]

    def _may_read(self, target: str | None) -> bool:
        """Ask whether the reader may read a target, by name, looking nothing up.

        Parameters
        ----------
        target
            The target's dotted name, or `None` when it cannot be told.

        Returns
        -------
        bool
            The table's READ_TABLE for a target that is a table name; READ_TABLE on the whole
            environment otherwise.
        """
        try:
            ref = None if target is None else TableRef.from_dotted(self._environment, target)
        except ValidationError:
            ref = None
        securable = (
            Securable.environment_scope(self._environment)
            if ref is None
            else Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        return self._authz.permits(self._principal, Action.READ_TABLE, securable)
