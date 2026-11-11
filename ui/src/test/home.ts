/**
 * What the server answers Home and the interface state with, for tests: a calm instance with
 * nothing to report, and realistic rows to add.
 *
 * @packageDocumentation
 */
import type { FakeRoute } from "./server";

/** A healthy instance as a person who may not manage environments sees it. */
export const HEALTHY = {
  state: "healthy",
  causes: [],
  workers_alive: 2,
  workers_total: 2,
  detail: null,
};

/** The detail an administrator sees: one of four slots busy, 1.2 GiB of 8 GiB. */
export const DETAIL = {
  slots_busy: 1,
  slots_total: 4,
  memory_used_bytes: 1.2 * 2 ** 30,
  memory_limit_bytes: 8 * 2 ** 30,
  workers: [],
};

/** The interface state of a person who saw the tour and nothing else. */
export const STATE = { tour_seen: true, get_started_hidden: false, update_note_seen: null };

/** A Get started list with every step done, so it shows no more. */
export const DONE = {
  audience: "admin",
  local: false,
  steps: [],
  hidden: false,
  complete: true,
};

/**
 * The routes Home and the interface state read, for a person by name, answering "nothing to
 * report".
 *
 * @param name - The person's principal name.
 * @returns The routes.
 */
export function homeRoutes(name: string): Record<string, FakeRoute> {
  return {
    "GET /api/v1/status": { status: 200, body: HEALTHY },
    "GET /api/v1/home/attention": { status: 200, body: { items: [], truncated: false } },
    "GET /api/v1/home/work": { status: 200, body: { repositories: [], jobs: [] } },
    "GET /api/v1/home/get-started": { status: 200, body: DONE },
    [`GET /api/v1/principals/${name}/interface-state`]: { status: 200, body: STATE },
    [`GET /api/v1/principals/${name}/recent`]: { status: 200, body: [] },
    "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
  };
}
