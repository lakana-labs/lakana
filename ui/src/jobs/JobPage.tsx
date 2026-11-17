/**
 * The job's page: its controls beside its name, its facts, its runs and the selected run.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useSearchParams } from "react-router";

import { recordOpened } from "../api";
import { getJob, type JobOut, type JobStartOut, type RunOut } from "../api/jobs";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { useLivePoll } from "../shell/useLivePoll";
import { ControlNote, type ControlNoteValue } from "./ControlNote";
import { RunNowButton, ScheduleButton } from "./JobControls";
import { JobFacts } from "./JobFacts";
import { JobRuns } from "./JobRuns";
import "./jobStrings";
import { jobRunPath } from "./paths";
import { LIST_EVERY_MS } from "./poll";
import { isGone, PollProblem } from "./PollProblem";
import { Reading } from "./Reading";
import { RunDetail } from "./RunDetail";

/** Properties of {@link JobView}. */
interface JobViewProps {
  /** The job's environment. */
  readonly env: string;
  /** The job's repository. */
  readonly repository: string;
  /** The job's name. */
  readonly job: string;
}

/**
 * One job, mounted once per job: read now and every 30 s while the tab is visible, never again
 * once it is gone, and recorded, once read, as opened for Home.
 *
 * @param props - The job the address names.
 * @param props.env - The job's environment.
 * @param props.repository - The job's repository.
 * @param props.job - The job's name.
 * @returns The page.
 */
function JobView({ env, repository, job }: JobViewProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const [stopped, setStopped] = useState(false);
  const load = useCallback(() => getJob(env, repository, job), [env, repository, job]);
  const poll = useLivePoll(load, { everyMs: LIST_EVERY_MS, active: !stopped });
  // A job that is gone is not read again.
  if (!stopped && isGone(poll)) {
    setStopped(true);
  }
  const [changed, setChanged] = useState<{ job: JobOut; over: JobOut | undefined } | null>(null);
  const [note, setNote] = useState<ControlNoteValue | null>(null);
  const [revision, setRevision] = useState(0);
  const [rows, setRows] = useState<readonly RunOut[] | null>(null);
  const read = poll.data;
  const shown = changed !== null && changed.over === read ? changed.job : read;

  const recorded = useRef(false);
  useEffect(() => {
    if (read !== undefined && !recorded.current) {
      recorded.current = true;
      // Recently opened is a convenience: a failed record is not the person's concern.
      recordOpened(me.name, "job", env, `${repository}/${job}`).catch(() => undefined);
    }
  }, [read, me.name, env, repository, job]);

  // The list follows the selected run: a run it does not hold, or holds in another state, makes
  // it read again (a new run, a run that ended).
  const listed = useRef(rows);
  useEffect(() => {
    listed.current = rows;
  }, [rows]);
  const onRunChanged = useCallback((run: RunOut) => {
    const shownRows = listed.current;
    if (
      shownRows !== null &&
      !shownRows.some((row) => row.id === run.id && row.status === run.status)
    ) {
      setRevision((previous) => previous + 1);
    }
  }, []);

  const select = (id: string): void => {
    setParams((current) => {
      current.set("run", id);
      return current;
    });
  };
  const onStarted = (result: JobStartOut): void => {
    if (result.run !== null) {
      select(result.run.id);
      setNote({ kind: "started", text: t("jobs.start.started", { job }) });
    } else if (result.waiting !== null) {
      setNote({
        kind: "waits",
        text: t("jobs.start.waits", { message: result.waiting.message }),
        to: jobRunPath(env, result.waiting.waits_for_run_id),
      });
    }
  };
  const selected = params.get("run") ?? rows?.[0]?.id ?? null;

  return (
    <Page
      title={job}
      trail={[
        { to: "/jobs", label: t("areas.jobs.name") },
        { to: "/jobs", label: env },
      ]}
      action={
        shown !== undefined && (
          <div className="jobs-head-actions">
            <RunNowButton environment={env} job={shown} primary onStarted={onStarted} />
            <ScheduleButton
              environment={env}
              job={shown}
              onChanged={(next) => {
                setChanged({ job: next, over: read });
                setNote({
                  kind: next.paused ? "paused" : "resumed",
                  text: t(next.paused ? "jobs.schedule.paused" : "jobs.schedule.resumed", {
                    job: next.name,
                  }),
                });
              }}
            />
          </div>
        )
      }
    >
      <ControlNote note={note} />
      <PollProblem poll={poll} />
      {shown === undefined ? (
        poll.error === null && <Reading label={t("jobs.loadingJob")} />
      ) : (
        <div className="jobs-panels">
          <JobFacts environment={env} job={shown} />
          <JobRuns
            environment={env}
            repository={repository}
            job={job}
            selected={selected}
            revision={revision}
            onRows={setRows}
          />
          {selected !== null && (
            <RunDetail environment={env} runId={selected} level={2} onRunChanged={onRunChanged} />
          )}
        </div>
      )}
    </Page>
  );
}

/**
 * Renders the job the address names. Run now is the page's one primary action; after it the new
 * run is selected (`?run=`) and the focus stays on the button. Without `?run=` the newest listed
 * run is selected. A job that is not deployed (any more) says so in the server's words.
 *
 * @returns The page.
 */
export function JobPage(): JSX.Element {
  const { env = "", repository = "", job = "" } = useParams();
  return (
    <JobView
      key={JSON.stringify([env, repository, job])}
      env={env}
      repository={repository}
      job={job}
    />
  );
}
