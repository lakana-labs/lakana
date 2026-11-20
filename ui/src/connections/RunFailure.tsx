/**
 * The newest run that failed on a connection: the failed glyph, words that link to the task, and
 * when it failed.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ConnectionRunFailureOut } from "../api";
import { relativeTime } from "../catalog/format";
import { taskPath } from "../jobs/paths";
import { useWhen } from "../shell/useWhen";
import { StatusGlyph } from "../status/StatusGlyph";
import "./connectionStrings";

/** Stands where the time goes while the line is split around it; a private-use character. */
const TIME_MARK = "";

/** Properties of {@link RunFailure}. */
export interface RunFailureProps {
  /** The connection's environment, where the run is. */
  readonly environment: string;
  /** The run that failed on it. */
  readonly failure: ConnectionRunFailureOut;
  /** The present, in milliseconds since the epoch. */
  readonly now: number;
  /** Whether to write the full date and name the task, as a connection's page does. */
  readonly full?: boolean;
}

/**
 * Renders "last run failed on it · 2 hours ago" with the failed glyph; the words link to the task
 * in its run, whose page says why. A list row writes the time relative to now; a connection's page
 * writes the full date and names the task.
 *
 * @param props - The failure and how to write it.
 * @param props.environment - The connection's environment.
 * @param props.failure - The run that failed on it.
 * @param props.now - The present.
 * @param props.full - Whether to write the full date and name the task.
 * @returns The line.
 */
export function RunFailure({
  environment,
  failure,
  now,
  full = false,
}: RunFailureProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const when = useWhen();
  const time = full
    ? when(failure.finished_at)
    : (relativeTime(failure.finished_at, now, i18n.language) ?? t("connections.status.justNow"));
  const [lead = "", trail = ""] = t("connections.runFailure.line", { when: TIME_MARK }).split(
    TIME_MARK,
  );
  const line = (
    <span className="test-status run-failure">
      <StatusGlyph kind="failed" />
      <Link to={taskPath(environment, failure.run_id, failure.asset)}>
        {lead}
        <time dateTime={failure.finished_at}>{time}</time>
        {trail}
      </Link>
    </span>
  );
  if (!full) {
    return line;
  }
  return (
    <>
      <p>{line}</p>
      <p className="second">{t("connections.runFailure.task", { asset: failure.asset })}</p>
    </>
  );
}
