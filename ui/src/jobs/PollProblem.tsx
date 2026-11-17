/**
 * A read of the Jobs screen that failed: the server's sentence, with Try again, or with the way
 * back to Jobs for something that is gone.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ReadFailed } from "../catalog/ReadFailed";
import { type LivePoll, POLL_FAILURES_SHOWN } from "../shell/useLivePoll";
import "./jobStrings";

/**
 * Whether a poll's object is gone or hidden from the person: its first read was refused with
 * 404 or 403. Reading again would not change that, so the page stops reading.
 *
 * @param poll - The poll.
 * @returns True when there is nothing to show.
 */
export function isGone(poll: LivePoll<unknown>): boolean {
  const status = poll.failure?.status;
  return poll.data === undefined && (status === 404 || status === 403);
}

/** Properties of {@link PollProblem}. */
export interface PollProblemProps {
  /** The poll. */
  readonly poll: LivePoll<unknown>;
}

/**
 * Says why a poll failed: before anything was read, at once; while older data is shown, once
 * {@link POLL_FAILURES_SHOWN} reads in a row failed. Something gone says the server's sentence
 * with the way back to Jobs; anything else offers Try again.
 *
 * @param props - The poll.
 * @param props.poll - The poll.
 * @returns The sentence, or nothing.
 */
export function PollProblem({ poll }: PollProblemProps): JSX.Element | null {
  const { t } = useTranslation();
  if (poll.error === null || (poll.data !== undefined && poll.failures < POLL_FAILURES_SHOWN)) {
    return null;
  }
  if (isGone(poll)) {
    return (
      <div className="read-failed">
        <p role="alert">{poll.error}</p>
        <Link className="button" to="/jobs">
          {t("jobs.toJobs")}
        </Link>
      </div>
    );
  }
  return <ReadFailed message={poll.error} onRetry={poll.reload} />;
}
