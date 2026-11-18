/**
 * What the server answers the Jobs list with, for tests: the environments `dev` and `prod`, no
 * run running, no failed run and no job, unless a test says otherwise.
 *
 * @packageDocumentation
 */
import type { FailedRunOut, JobOut, RunOut } from "../api/jobs";
import { ENVIRONMENTS } from "./jobs";
import type { FakeRoute } from "./server";

/** What the list's routes answer; each part empty by default. */
export interface JobsListAnswers {
  /** The runs `GET /api/v1/runs` lists. */
  readonly running?: readonly RunOut[];
  /** The cursor of the next page of running runs. */
  readonly runningNext?: string | null;
  /** The failed runs. */
  readonly failed?: readonly FailedRunOut[];
  /** Whether the server cut the failed runs. */
  readonly truncated?: boolean;
  /** The jobs of `dev`. */
  readonly dev?: readonly JobOut[];
  /** The jobs of `prod`. */
  readonly prod?: readonly JobOut[];
}

/**
 * The routes of the Jobs list.
 *
 * @param answers - What differs from an empty instance with `dev` and `prod`.
 * @returns The routes.
 */
export function jobsListRoutes(answers: JobsListAnswers = {}): Record<string, FakeRoute> {
  return {
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    "GET /api/v1/runs": {
      status: 200,
      body: { items: answers.running ?? [], next_cursor: answers.runningNext ?? null },
    },
    "GET /api/v1/runs/failed": {
      status: 200,
      body: { items: answers.failed ?? [], truncated: answers.truncated ?? false },
    },
    "GET /api/v1/environments/dev/jobs": { status: 200, body: answers.dev ?? [] },
    "GET /api/v1/environments/prod/jobs": { status: 200, body: answers.prod ?? [] },
  };
}
