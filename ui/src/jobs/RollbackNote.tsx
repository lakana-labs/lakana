/**
 * The note under the Deploys list that follows a rollback the person asked for to its end.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getDeploy } from "../api/jobs";
import { useLivePoll } from "../shell/useLivePoll";
import "./deployStrings";
import { LIVE_EVERY_MS } from "./poll";
import { PollProblem } from "./PollProblem";

/** Properties of {@link RollbackNote}. */
export interface RollbackNoteProps {
  /** The environment. */
  readonly environment: string;
  /** The repository rolled back. */
  readonly repository: string;
  /** The commit it is rolled back to, as the list writes it. */
  readonly commit: string;
  /** The rollback's id. */
  readonly deployId: string;
  /** Told once the rollback succeeded or failed, so the list is read again. */
  readonly onEnded: () => void;
}

/**
 * Renders the note, inside the list's status region: "Rolling back…" while the rollback is queued or running, reading it every
 * 2 s while the tab is visible (never in a hidden tab, at once when it shows again), then "Rolled
 * back…", or the failure with the server's first line as an alert; once it ended it reads no
 * more and says so to the list once. A read that keeps failing says why, with Try again.
 *
 * @param props - The rollback and what to tell of its end.
 * @param props.environment - The environment.
 * @param props.repository - The repository.
 * @param props.commit - The commit.
 * @param props.deployId - The rollback's id.
 * @param props.onEnded - Told once it ended.
 * @returns The note.
 */
export function RollbackNote({
  environment,
  repository,
  commit,
  deployId,
  onEnded,
}: RollbackNoteProps): JSX.Element {
  const { t } = useTranslation();
  const load = useCallback(() => getDeploy(environment, deployId), [environment, deployId]);
  // Once over, the poll stops: an ended rollback does not change.
  const [done, setDone] = useState(false);
  const poll = useLivePoll(load, { everyMs: LIVE_EVERY_MS, active: !done });
  const deploy = poll.data;
  const over = deploy?.status === "succeeded" || deploy?.status === "failed";
  if (over && !done) {
    setDone(true);
  }
  const told = useRef(false);
  useEffect(() => {
    if (done && !told.current) {
      told.current = true;
      onEnded();
    }
  }, [done, onEnded]);
  const names = { repository, environment, commit };
  if (deploy?.status === "failed") {
    const message = deploy.error_message?.split("\n")[0]?.trim() ?? "";
    return (
      <p className="jobs-note" role="alert">
        {message === ""
          ? t("jobs.deploys.note.failedWithout")
          : t("jobs.deploys.note.failed", { message })}
      </p>
    );
  }
  return (
    <>
      <p className="jobs-note">
        {t(
          deploy?.status === "succeeded" ? "jobs.deploys.note.done" : "jobs.deploys.note.rolling",
          names,
        )}
      </p>
      <PollProblem poll={poll} />
    </>
  );
}
