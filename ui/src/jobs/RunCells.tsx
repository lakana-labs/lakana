/**
 * The first cells of a run's row, which the Jobs list's sections and the Runs tab share: its
 * state, its name as the link to its page, and its environment.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-router";

import type { RunOut } from "../api/jobs";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import { jobRunPath } from "./paths";
import { runStatusKind } from "./status";

/**
 * What a run is called in a row and in a control's name: its job, or its repository for a run of
 * no job.
 *
 * @param run - The run.
 * @returns The name.
 */
export function runName(run: RunOut): string {
  return run.job ?? run.repository;
}

/** Properties of {@link RunCells}. */
export interface RunCellsProps {
  /** The run. */
  readonly run: RunOut;
}

/**
 * The first three cells of a run's row: its state in a glyph and a word, its name as the link to
 * the run's page, and its environment.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The three cells.
 */
export function RunCells({ run }: RunCellsProps): JSX.Element {
  return (
    <>
      <td>
        <StatusLabel kind={runStatusKind(run.status)} />
      </td>
      <td>
        <Link className="row-name" to={jobRunPath(run.environment, run.id)}>
          <VisibleText text={runName(run)} />
        </Link>
      </td>
      <td>
        <EnvironmentLabel environment={run.environment} />
      </td>
    </>
  );
}
