/**
 * A task's log, followed while the task runs: read piece by piece from the byte offset the server
 * gives, kept to a size the page can hold.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getTaskLog, type TaskAttemptOut } from "../api/jobs";
import { POLL_FAILURES_SHOWN, useLivePoll } from "../shell/useLivePoll";
import { LIVE_EVERY_MS } from "./poll";
import { isGone } from "./PollProblem";

/** The most characters of a log the page keeps; past it, whole lines go from the start. */
export const LOG_KEEP_CHARS = 2_000_000;

/** The most pieces read one after the other while the server has more; past it, following stops. */
export const LOG_CATCHUP_PIECES = 16;

/**
 * What a cut keeps: a tenth below {@link LOG_KEEP_CHARS}, so that the next cut comes only after
 * that much new text. Between two cuts the text only grows at its end, and the parts of it the
 * page has drawn stay as they are (see `LogView`).
 */
const LOG_CUT_TO = LOG_KEEP_CHARS - LOG_KEEP_CHARS / 10;

/** A task's log as the page shows it. */
export interface TaskLog {
  /** The text read so far, at most {@link LOG_KEEP_CHARS} characters. */
  readonly text: string;
  /** The task's attempts, as the latest piece listed them. */
  readonly attempts: readonly TaskAttemptOut[];
  /** Whether the task has finished and the text reaches the end of its log. */
  readonly complete: boolean;
  /** Whether lines were dropped from the start to keep the text within the limit. */
  readonly cut: boolean;
  /** Whether following stopped because the log grew faster than the page may read it. */
  readonly stopped: boolean;
  /** The server's sentence once the reads failed, as `PollProblem` would show it; else null. */
  readonly error: string | null;
  /** Whether the task is gone or hidden from the person (404 or 403); nothing more is read. */
  readonly gone: boolean;
  /** Reads again at once, after a failure. */
  readonly retry: () => void;
}

/** What has been read, and where the next read starts. */
interface Followed {
  /** The text kept. */
  readonly text: string;
  /** The attempts of the latest piece. */
  readonly attempts: readonly TaskAttemptOut[];
  /** The byte offset to read from next. */
  readonly offset: number;
  /** Whether the log is complete. */
  readonly complete: boolean;
  /** Whether lines were dropped. */
  readonly cut: boolean;
  /** Whether following stopped. */
  readonly stopped: boolean;
  /** Whether any piece was read. */
  readonly read: boolean;
}

const NOTHING_READ: Followed = {
  text: "",
  attempts: [],
  offset: 0,
  complete: false,
  cut: false,
  stopped: false,
  read: false,
};

/**
 * Keeps a text within {@link LOG_KEEP_CHARS}: past it, drops whole lines from the start until
 * at most {@link LOG_CUT_TO} characters are left. A last line longer than that keeps its end.
 *
 * @param text - The text.
 * @returns The text kept.
 */
function keep(text: string): string {
  if (text.length <= LOG_KEEP_CHARS) {
    return text;
  }
  const from = text.length - LOG_CUT_TO;
  const lineEnd = text.indexOf("\n", from - 1);
  return text.slice(lineEnd === -1 || lineEnd === text.length - 1 ? from : lineEnd + 1);
}

/**
 * Whether the page is shown.
 *
 * @returns True while the tab is visible.
 */
function visible(): boolean {
  return document.visibilityState === "visible";
}

/**
 * Follows a task's log. It reads from offset 0, then at once each next piece while the server
 * has more (at most {@link LOG_CATCHUP_PIECES} in a row; past that it stops following), then
 * every two seconds until the log is complete, and never again after. A `reset` (the offset lay
 * beyond the end of a log written anew) drops the text and reads the log again from offset 0, at
 * once, as a fresh log. Nothing is read while the tab is hidden: a catch-up stops there, and the
 * reads go on when it shows. The reads fail and back off as `useLivePoll`'s do; a read that Try
 * again overtook is dropped. Mount it once per task (a `key`).
 *
 * @param environment - The run's environment.
 * @param runId - The run's id.
 * @param asset - The task's asset.
 * @returns The log.
 */
export function useTaskLog(environment: string, runId: string, asset: string): TaskLog {
  const followed = useRef(NOTHING_READ);
  const [shown, setShown] = useState(NOTHING_READ);
  // Each load takes the next number; a load whose number is no longer the latest (Try again
  // started another, or the page was left) drops what it read and stops, so two loads never
  // append the same piece.
  const latest = useRef(0);
  useEffect(
    () => () => {
      latest.current += 1;
    },
    [],
  );
  const load = useCallback(async (): Promise<Followed> => {
    latest.current += 1;
    const mine = latest.current;
    for (let pieces = 1; ; pieces += 1) {
      const piece = await getTaskLog(environment, runId, asset, followed.current.offset);
      if (mine !== latest.current) {
        break;
      }
      // A reset means the log was written anew: drop the text and read it from its start.
      const before = piece.reset ? NOTHING_READ : followed.current;
      const text = piece.reset ? "" : keep(before.text + piece.text);
      const more = piece.more || piece.reset;
      const next: Followed = {
        text,
        attempts: piece.attempts,
        offset: piece.reset ? 0 : piece.next_offset,
        complete: piece.complete && !piece.reset,
        cut: before.cut || text.length < before.text.length + piece.text.length,
        stopped: more && pieces >= LOG_CATCHUP_PIECES,
        read: true,
      };
      followed.current = next;
      setShown(next);
      if (!more || next.stopped || !visible()) {
        break;
      }
    }
    return followed.current;
  }, [environment, runId, asset]);
  const [gone, setGone] = useState(false);
  const poll = useLivePoll(load, {
    everyMs: LIVE_EVERY_MS,
    active: !shown.complete && !shown.stopped && !gone,
  });
  if (!gone && !shown.read && isGone(poll)) {
    setGone(true);
  }
  const failing = !shown.read || poll.failures >= POLL_FAILURES_SHOWN;
  return {
    text: shown.text,
    attempts: shown.attempts,
    complete: shown.complete,
    cut: shown.cut,
    stopped: shown.stopped,
    error: failing ? poll.error : null,
    gone,
    retry: poll.reload,
  };
}
