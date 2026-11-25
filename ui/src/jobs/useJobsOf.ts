/**
 * The jobs of several environments, read side by side and kept apart when one cannot be read.
 *
 * @packageDocumentation
 */
import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import { type JobOut, listJobs } from "../api/jobs";
import type { ListSortChoice } from "../api/sorting";
import type { LivePoll } from "../shell/useLivePoll";
import { problemText } from "../sign-in/messages";
import "./jobStrings";
import "./listStrings";
import { useListPoll } from "./useListPoll";

/** How many environments' jobs are read at once, at most. */
const JOBS_READS_IN_FLIGHT = 4;

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
  /** The jobs of every environment read, in the sort asked for (`jobOrder`). */
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
 * The instant of an ISO time, or null without one.
 *
 * @param iso - The time.
 * @returns Milliseconds since the epoch.
 */
function instant(iso: string | null): number | null {
  return iso === null ? null : Date.parse(iso);
}

/**
 * The value of a job a sort other than by name compares: a state word as text, a time as its
 * instant (a whole second is written without a fraction, so its text would sort after a later
 * time of the same second).
 */
const SORT_VALUES: Readonly<Record<string, (job: JobOut) => string | number | null>> = {
  status: (job) => job.last_run_status,
  last_run: (job) => instant(job.last_run_at),
  next_run: (job) => instant(job.next_run_at),
};

/**
 * Orders the jobs of several environments the way the server orders one environment's: by the
 * field, a job without a value last in either direction, equal values by name, repository and
 * environment. Each environment's list comes sorted; this merges them.
 *
 * @param sort - The sort; null for the name, A to Z.
 * @returns The order.
 */
export function jobOrder(sort: ListSortChoice | null): (a: JobOut, b: JobOut) => number {
  const sign = sort?.order === "desc" ? -1 : 1;
  const value = sort === null ? undefined : SORT_VALUES[sort.sort];
  if (value === undefined) {
    return (a, b) => sign * byName(a, b);
  }
  return (a, b) => {
    const left = value(a);
    const right = value(b);
    if (left === right) {
      return byName(a, b);
    }
    if (left === null || right === null) {
      return left === null ? 1 : -1;
    }
    return sign * (left < right ? -1 : 1);
  };
}

/**
 * Reads each environment's jobs, at most {@link JOBS_READS_IN_FLIGHT} at once, every 30 s while
 * the tab is visible and at once when `reloadKey` changes. An environment whose read fails keeps
 * its place among `failed` with its sentence; the others' jobs stay.
 *
 * @param environments - The environments to read, in the order the failures are listed.
 * @param reloadKey - A number that changes when the jobs must be read again.
 * @param sort - The sort each environment's jobs are read in and the merged rows are put in;
 *   null for the name.
 * @returns The jobs, the failures and whether the first read is open.
 */
export function useJobsOf(
  environments: readonly string[],
  reloadKey: number,
  sort: ListSortChoice | null = null,
): JobsOf {
  const { t } = useTranslation();
  const key = JSON.stringify(environments);
  const load = useCallback(async (): Promise<Round> => {
    const names = JSON.parse(key) as string[];
    const answers: ({ readonly jobs: readonly JobOut[] } | { readonly message: string })[] = [];
    let next = 0;
    const worker = async (): Promise<void> => {
      while (next < names.length) {
        const index = next;
        next += 1;
        try {
          answers[index] = { jobs: await listJobs(names[index] as string, undefined, sort) };
        } catch (failure: unknown) {
          answers[index] = { message: problemText(t, failure) };
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(JOBS_READS_IN_FLIGHT, names.length) }, worker));
    const rows: JobOut[] = [];
    const failed: { environment: string; message: string }[] = [];
    answers.forEach((answer, index) => {
      if ("jobs" in answer) {
        rows.push(...answer.jobs);
      } else {
        failed.push({ environment: names[index] as string, message: answer.message });
      }
    });
    return { rows: rows.sort(jobOrder(sort)), failed };
  }, [key, t, sort]);
  const poll = useListPoll(load, reloadKey);
  return {
    rows: poll.data?.rows ?? [],
    failed: (poll.data?.failed ?? []).map((failure) => ({ ...failure, retry: poll.reload })),
    loading: poll.data === undefined,
    poll,
  };
}
