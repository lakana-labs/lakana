/**
 * Show rows of a failed check: the rows the check found wrong, in the Queries screen's grid.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { type CheckResultOut, showCheckRows } from "../api/jobs";
import { runPath } from "../queries/paths";
import { ControlButton } from "./ControlNote";
import "./jobStrings";

/** Properties of {@link ShowRowsButton}. */
export interface ShowRowsButtonProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run's id. */
  readonly runId: string;
  /** The failed check. */
  readonly check: CheckResultOut;
}

/**
 * Asks the server to run the query that finds the check's failing rows, then opens that query's
 * run on the Queries screen. The page cannot tell whether the person may run queries, so the
 * button is offered for every failed check; a refusal shows the server's sentence beside it.
 *
 * @param props - The run and the check.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.check - The failed check.
 * @returns The button.
 */
export function ShowRowsButton({ environment, runId, check }: ShowRowsButtonProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <ControlButton
      label={t("jobs.task.showRows", { check: check.name })}
      act={async () => {
        const queryId = await showCheckRows(environment, runId, check.name);
        await navigate(runPath(environment, queryId));
      }}
    />
  );
}
