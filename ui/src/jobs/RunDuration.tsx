/**
 * How long a run took, for the cell of a run list.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { RunOut } from "../api/jobs";
import { Unknown } from "../catalog/cells";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";

/**
 * A run's duration: the time between its start and its end, or up to now while it runs. A queued
 * run says it has not started; a run that ended without starting, such as one cancelled while
 * queued, has nothing measured and shows a plain dash, never a time.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The duration, or a dash with its words.
 */
export function RunDuration({
  run,
}: {
  readonly run: Pick<RunOut, "status" | "started_at" | "finished_at">;
}): JSX.Element {
  const { t } = useTranslation();
  if (run.started_at === null) {
    return (
      <Unknown
        words={t(
          run.status === "queued" ? "jobs.duration.notStarted" : "jobs.attempts.notMeasured",
        )}
      />
    );
  }
  return <>{formatDuration(secondsBetween(run.started_at, run.finished_at ?? undefined), t)}</>;
}
