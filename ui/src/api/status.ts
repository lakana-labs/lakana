/**
 * Whether the instance works: healthy, degraded or down, and why.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** A registered worker, as far as Home reads it. */
interface StatusWorkerOut {
  /** The worker's id. */
  readonly id: string;
  /** Its host name. */
  readonly host: string;
  /** `alive` or `lost`. */
  readonly status: string;
  /** True for a lost worker that a restart on its host replaced. */
  readonly replaced: boolean;
}

/** What an administrator sees of the instance beyond its state. */
export interface StatusDetailOut {
  /** The busy task slots of the alive workers. */
  readonly slots_busy: number;
  /** All task slots of the alive workers. */
  readonly slots_total: number;
  /** The memory the alive workers last reported, added up. */
  readonly memory_used_bytes: number;
  /** Their limits, added up; null when a worker reports none or none is alive. */
  readonly memory_limit_bytes: number | null;
  /** Every registered worker. */
  readonly workers: readonly StatusWorkerOut[];
}

/** The instance's state as `GET /api/v1/status` answers it for the caller. */
export interface InstanceStatusOut {
  /** `healthy`, `degraded` (work gets done, slower or at risk) or `down`. */
  readonly state: "healthy" | "degraded" | "down";
  /**
   * Why, in this order: `database`, `storage`, `no_worker`, `worker_lost`, `memory_high`; a
   * caller who may not manage the instance sees only `no_worker` and `worker_lost`.
   */
  readonly causes: readonly string[];
  /** The workers whose heartbeat is younger than the lease. */
  readonly workers_alive: number;
  /** Every registered worker but those a restart replaced. */
  readonly workers_total: number;
  /** Compute, memory and the workers; null unless the caller may manage the instance. */
  readonly detail: StatusDetailOut | null;
}

/**
 * Reads the instance's status; a background request, since Home polls it.
 *
 * @returns The status as the caller may see it.
 */
export async function getStatus(): Promise<InstanceStatusOut> {
  return api.request<InstanceStatusOut>("GET", "/api/v1/status", undefined, { background: true });
}
