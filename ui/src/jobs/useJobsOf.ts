/**
 * The jobs of several environments, read side by side and kept apart when one cannot be read.
 *
 * @packageDocumentation
 */
import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import type { JobOut } from "../api/jobs";
import type { LivePoll } from "../shell/useLivePoll";
import { problemText } from "../sign-in/messages";
import "./jobStrings";
import "./listStrings";
import { readJobsOf } from "./readJobsOf";
import { useListPoll } from "./useListPoll";

/** An environment whose jobs could not be read. */
interface JobsReadFailure {
  /** The environment. */
  readonly environment: string;
  /** The server's sentence, or the interface's, with the cause and the next step. */
  readonly message: string;
  /** Reads the jobs again. */
  readonly retry: () => void;
}

/** What {@link useJobsOf} read. */
export interface JobsOf {
  /** The jobs of every environment read, by name, then repository, then environment. */
  readonly rows: readonly JobOut[];
  /** The environments whose jobs could not be read, in the order asked for. */
  readonly failed: readonly JobsReadFailure[];
  /** Whether the first read is still open. */
  readonly loading: boolean;
  /** The poll behind it. */
  readonly poll: LivePoll<unknown>;
}

/** One read of every environment: their jobs, and the sentence of each that failed. */
interface Round {
  /** The jobs read. */
  readonly rows: readonly JobOut[];
  /** The environments that failed, with the sentence. */
  readonly failed: readonly { readonly environment: string; readonly message: string }[];
}

/**
 * Orders jobs by name, then repository, then environment.
 *
 * @param a - One job.
 * @param b - The other.
 * @returns Their order.
 */
function byName(a: JobOut, b: JobOut): number {
  return (
    a.name.localeCompare(b.name) ||
    a.repository.localeCompare(b.repository) ||
    a.environment.localeCompare(b.environment)
  );
}

/**
 * Reads each environment's jobs, at most {@link JOBS_READS_IN_FLIGHT} at once, every 30 s while
 * the tab is visible and at once when `reloadKey` changes. An environment whose read fails keeps
 * its place among `failed` with its sentence; the others' jobs stay.
 *
 * @param environments - The environments to read, in the order the failures are listed.
 * @param reloadKey - A number that changes when the jobs must be read again.
 * @returns The jobs, the failures and whether the first read is open.
 */
export function useJobsOf(environments: readonly string[], reloadKey: number): JobsOf {
  const { t } = useTranslation();
  const key = JSON.stringify(environments);
  const load = useCallback(async (): Promise<Round> => {
    const names = JSON.parse(key) as string[];
    const answers = await readJobsOf(names);
    const rows: JobOut[] = [];
    const failed: { environment: string; message: string }[] = [];
    answers.forEach((answer, index) => {
      if ("jobs" in answer) {
        rows.push(...answer.jobs);
      } else {
        failed.push({
          environment: names[index] as string,
          message: problemText(t, answer.failure),
        });
      }
    });
    return { rows: rows.sort(byName), failed };
  }, [key, t]);
  const poll = useListPoll(load, reloadKey);
  return {
    rows: poll.data?.rows ?? [],
    failed: (poll.data?.failed ?? []).map((failure) => ({ ...failure, retry: poll.reload })),
    loading: poll.data === undefined,
    poll,
  };
}
