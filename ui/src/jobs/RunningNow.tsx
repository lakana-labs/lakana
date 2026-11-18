/**
 * Running now: the queued and running runs of every environment the person may read, or of the
 * one chosen, each with its progress and Cancel.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ACTIVE_RUN_STATUSES, listAllRuns, type RunOut } from "../api/jobs";
import type { CursorPage } from "../api/manage";
import { useWhen } from "../shell/useWhen";
import type { ControlNoteValue } from "./ControlNote";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";
import "./listStrings";
import { ListSection, RowEnd } from "./ListSection";
import { runsTabPath } from "./paths";
import { RunCells, RunHeadCells, runName } from "./RunCells";
import { CancelRunButton } from "./RunControls";
import { matchesSearch } from "./useJobsFilter";
import { useListPoll } from "./useListPoll";

/** Properties of the Jobs list's sections. */
export interface SectionProps {
  /** The environment chosen; null for every environment the person may read. */
  readonly environment: string | null;
  /** The words the list is searched for. */
  readonly query: string;
  /** A number that changes when the section must be read again at once. */
  readonly reloadKey: number;
  /** Told that a control of the section acted, so the list is read again. */
  readonly onChanged: () => void;
}

/** Properties of {@link RunProgress}. */
interface RunProgressProps {
  /** The run. */
  readonly run: RunOut;
}

/**
 * How far a run is: "started 02:00 · 14 min · 3 of 7 tasks", or "queued 02:00" before it starts.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The text.
 */
function RunProgress({ run }: RunProgressProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  if (run.started_at === null) {
    return <>{t("jobs.list.running.queued", { time: when(run.created_at) })}</>;
  }
  return (
    <>
      {t("jobs.list.running.progress", {
        time: when(run.started_at),
        duration: formatDuration(secondsBetween(run.started_at), t),
        ended: run.ended_tasks,
        total: run.total_tasks,
      })}
    </>
  );
}

/**
 * Renders Running now: the first 50 runs, newest first, read every 30 s while the tab is visible;
 * a line says when more run than shown. Each row names its state in a glyph and a word, opens the
 * run's page from its job's name (its repository's for a run of no job), names its environment
 * and its progress, and offers Cancel when the server lists it. A cancel the server refuses (a run
 * that ended meanwhile) closes the question, says the server's sentence under the section and
 * reads the list again.
 *
 * @param props - The environment, the search, the reload key and what to tell of a change.
 * @param props.environment - The environment chosen.
 * @param props.query - The words searched for.
 * @param props.reloadKey - Changes when the section must be read again.
 * @param props.onChanged - Told that a control acted.
 * @returns The section.
 */
export function RunningNow({
  environment,
  query,
  reloadKey,
  onChanged,
}: SectionProps): JSX.Element {
  const { t } = useTranslation();
  const load = useCallback(
    () =>
      listAllRuns(
        {
          environment,
          status: ACTIVE_RUN_STATUSES,
          trigger: [],
          job: null,
          repository: null,
          since: null,
        },
        null,
      ),
    [environment],
  );
  const poll = useListPoll(load, reloadKey);
  const [note, setNote] = useState<ControlNoteValue | null>(null);
  // A cancelled run as the server answered the cancel, until the list is read again.
  const [changed, setChanged] = useState<{
    readonly run: RunOut;
    readonly over: CursorPage<RunOut> | undefined;
  } | null>(null);
  const page = poll.data;
  const rows = (page?.items ?? [])
    .map((run) =>
      changed !== null && changed.over === page && changed.run.id === run.id ? changed.run : run,
    )
    .filter((run) => matchesSearch(query, run.job, run.repository));
  const more = page?.nextCursor !== null && page?.nextCursor !== undefined;
  return (
    <ListSection
      heading={t(more ? "jobs.list.running.headingMore" : "jobs.list.running.heading", {
        count: rows.length,
      })}
      poll={poll}
      loading={t("jobs.list.running.loading")}
      rowCount={rows.length}
      empty={<p>{t(query.trim() === "" ? "jobs.list.running.empty" : "jobs.list.noMatch")}</p>}
      region={t("jobs.list.running.region")}
      scroll="jobs-running-scroll"
      note={note}
      after={
        more && (
          <p className="jobs-list-more">
            {t("jobs.list.running.more")}{" "}
            <Link to={runsTabPath({ environment, status: ACTIVE_RUN_STATUSES })}>
              {t("jobs.list.running.seeAll")}
            </Link>
          </p>
        )
      }
      head={
        <>
          <RunHeadCells />
          <th scope="col">{t("jobs.list.columns.progress")}</th>
        </>
      }
    >
      {rows.map((run) => (
        <tr key={run.id}>
          <RunCells run={run} />
          <td>
            <RunProgress run={run} />
          </td>
          <RowEnd>
            <CancelRunButton
              environment={run.environment}
              run={run}
              name={runName(run)}
              inRow
              onChanged={(next) => {
                setChanged({ run: next, over: page });
                setNote(null);
                onChanged();
              }}
              onRefused={(message) => {
                setNote({ kind: "refused", text: message });
                onChanged();
              }}
            />
          </RowEnd>
        </tr>
      ))}
    </ListSection>
  );
}
