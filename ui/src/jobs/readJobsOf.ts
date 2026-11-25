/**
 * Reading the jobs of several environments: one request per environment, a few at once, as All
 * jobs and the Runs tab's waiting starts both do.
 *
 * @packageDocumentation
 */
import { type JobOut, listJobs } from "../api/jobs";

/** The most environments whose jobs are read at once, so many environments do not flood. */
export const JOBS_READS_IN_FLIGHT = 4;

/** What the read of one environment's jobs came to. */
export type JobsAnswer = { readonly jobs: readonly JobOut[] } | { readonly failure: unknown };

/**
 * Settles after `ms` with a failure, so a read that hangs is given up like one that failed.
 *
 * @param read - The read.
 * @param ms - The time it may take; undefined for no limit.
 * @returns The read's answer, or a rejection once the time is up.
 */
function within<T>(read: Promise<T>, ms: number | undefined): Promise<T> {
  if (ms === undefined) {
    return read;
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(`No answer within ${String(ms)} ms.`));
    }, ms);
  });
  return Promise.race([read, late]).finally(() => {
    clearTimeout(timer);
  });
}

/**
 * Reads the jobs of each environment, at most {@link JOBS_READS_IN_FLIGHT} at once, and answers
 * them in the order of `names`. A read that fails, or that takes longer than `timeoutMs`, is a
 * failure of its environment alone; the others' jobs still come.
 *
 * @param names - The environments.
 * @param timeoutMs - How long one environment's read may take; undefined for no limit.
 * @returns One answer per environment, in order.
 */
export async function readJobsOf(
  names: readonly string[],
  timeoutMs?: number,
): Promise<JobsAnswer[]> {
  const answers: JobsAnswer[] = [];
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < names.length) {
      const index = next;
      next += 1;
      try {
        answers[index] = { jobs: await within(listJobs(names[index] as string), timeoutMs) };
      } catch (failure: unknown) {
        answers[index] = { failure };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(JOBS_READS_IN_FLIGHT, names.length) }, worker));
  return answers;
}
