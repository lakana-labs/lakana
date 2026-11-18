/**
 * A run as the run page and the task page read it: again every two seconds while it is queued or
 * running, with its title and the breadcrumb above it.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { useCallback, useState } from "react";

import { getRun, type RunOut } from "../api/jobs";
import type { Crumb } from "../shell/Breadcrumbs";
import { type LivePoll, useLivePoll } from "../shell/useLivePoll";
import "./jobStrings";
import { jobPath } from "./paths";
import { LIVE_EVERY_MS } from "./poll";
import { isGone } from "./PollProblem";
import { isActive } from "./status";

/**
 * Reads a run now and every two seconds while it is queued or running; never again once it has
 * ended, or once it is gone (404 or 403 before anything was read).
 *
 * @param environment - The run's environment.
 * @param runId - The run's id.
 * @returns The poll.
 */
export function useRun(environment: string, runId: string): LivePoll<RunOut> {
  const [ended, setEnded] = useState(false);
  const load = useCallback(() => getRun(environment, runId), [environment, runId]);
  const poll = useLivePoll(load, { everyMs: LIVE_EVERY_MS, active: !ended });
  if (!ended && ((poll.data !== undefined && !isActive(poll.data)) || isGone(poll))) {
    setEnded(true);
  }
  return poll;
}

/**
 * A run's title: "Run of {when}", with the repository for a run of no job, since a repository
 * has no page here; "Run" while it is not read.
 *
 * @param run - The run, or undefined while it is read.
 * @param t - The translation function.
 * @param when - Writes a timestamp, as `useWhen` gives it.
 * @returns The title.
 */
export function runTitle(
  run: RunOut | undefined,
  t: TFunction,
  when: (iso: string) => string,
): string {
  if (run === undefined) {
    return t("jobs.run.untitled");
  }
  return run.job === null
    ? t("jobs.run.titleOf", { when: when(run.created_at), repository: run.repository })
    : t("jobs.run.title", { when: when(run.created_at) });
}

/**
 * The breadcrumb's parts down to a run: Jobs, the environment and the run's job, which a run of
 * no job (or one not read yet) leaves out.
 *
 * @param environment - The run's environment.
 * @param run - The run, or undefined while it is read.
 * @param t - The translation function.
 * @returns The parts.
 */
export function runTrail(environment: string, run: RunOut | undefined, t: TFunction): Crumb[] {
  return [
    { to: "/jobs", label: t("areas.jobs.name") },
    { to: "/jobs", label: environment },
    ...(run === undefined || run.job === null
      ? []
      : [{ to: jobPath(environment, run.repository, run.job), label: run.job }]),
  ];
}
