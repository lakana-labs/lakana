/**
 * Failed, needs attention: the runs Home's Needs your attention lists, each with why it failed,
 * Resume and Logs.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listFailedRuns } from "../api/jobs";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import type { ControlNoteValue } from "./ControlNote";
import { FailureLine } from "./FailureLine";
import "./jobStrings";
import "./listStrings";
import { ListSection, RowEnd } from "./ListSection";
import { jobRunPath, taskPath } from "./paths";
import { ResumeRunButton } from "./RunControls";
import { RunCells, runName, type SectionProps } from "./RunningNow";
import { matchesSearch } from "./useJobsFilter";
import { useListPoll } from "./useListPoll";

/**
 * Renders the section: exactly the server's rows, in its order (the rule Home's Needs your
 * attention uses, so the two never disagree), narrowed only by the search, read every 30 s while
 * the tab is visible. Each row names its job (its repository for a run of no job) as the link to
 * the run, its environment, when it ended and why it failed: the failing task's code, cause and
 * next step, the run's own message when no task failed, or else the reference's `internal`.
 * Resume is offered when the server lists it; Logs opens the failing task, or the run without
 * one. A line says when the server cut the list.
 *
 * @param props - The environment, the search, the reload key and what to tell of a change.
 * @param props.environment - The environment chosen.
 * @param props.query - The words searched for.
 * @param props.reloadKey - Changes when the section must be read again.
 * @param props.onChanged - Told that a control acted.
 * @returns The section.
 */
export function FailedRuns({
  environment,
  query,
  reloadKey,
  onChanged,
}: SectionProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback(() => listFailedRuns(environment), [environment]);
  const poll = useListPoll(load, reloadKey);
  const [note, setNote] = useState<ControlNoteValue | null>(null);
  const rows = (poll.data?.items ?? []).filter((row) =>
    matchesSearch(query, row.run.job, row.run.repository),
  );
  return (
    <ListSection
      heading={t("jobs.list.failed.heading", { count: rows.length })}
      poll={poll}
      loading={t("jobs.list.failed.loading")}
      rowCount={rows.length}
      empty={<p>{t(query.trim() === "" ? "jobs.list.failed.empty" : "jobs.list.noMatch")}</p>}
      region={t("jobs.list.failed.region")}
      scroll="jobs-failed-scroll"
      note={note}
      after={
        poll.data?.truncated === true && (
          <p className="jobs-list-more">{t("jobs.list.failed.truncated")}</p>
        )
      }
      head={
        <>
          <th scope="col">{t("jobs.list.columns.state")}</th>
          <th scope="col">{t("jobs.list.columns.job")}</th>
          <th scope="col">{t("jobs.list.columns.environment")}</th>
          <th scope="col">{t("jobs.list.columns.ended")}</th>
          <th scope="col">{t("jobs.list.columns.why")}</th>
        </>
      }
    >
      {rows.map(({ run, task, failure }) => (
        <tr key={run.id}>
          <RunCells run={run} />
          <td>
            {run.finished_at !== null && (
              <time dateTime={run.finished_at}>{when(run.finished_at)}</time>
            )}
          </td>
          <td className="jobs-why-cell">
            {failure === null && run.error_message !== null ? (
              <VisibleText text={run.error_message} />
            ) : (
              <FailureLine
                failure={
                  failure ?? {
                    // Neither a failed task nor the run's own message: the reference's
                    // `internal`, so the row never fails without saying why.
                    code: "internal",
                    cause: t("jobs.list.failed.unknownCause"),
                    next_step: t("jobs.list.failed.unknownNextStep"),
                  }
                }
                inline
              />
            )}
          </td>
          <RowEnd>
            <ResumeRunButton
              environment={run.environment}
              run={run}
              name={runName(run)}
              onResumed={() => {
                setNote({ kind: "resumed", text: t("jobs.resume.done") });
                onChanged();
              }}
            />
            <Link
              className="button"
              to={
                task === null
                  ? jobRunPath(run.environment, run.id)
                  : taskPath(run.environment, run.id, task)
              }
              aria-label={t("jobs.list.failed.logsName", { name: runName(run) })}
            >
              {t("jobs.run.logs")}
            </Link>
          </RowEnd>
        </tr>
      ))}
    </ListSection>
  );
}
