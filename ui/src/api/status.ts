/**
 * Whether the instance works: healthy, degraded or down, and why.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** The memory a worker last reported. */
interface WorkerMemoryOut {
  /** The bytes in use. */
  readonly used_bytes: number;
  /** Its limit; null when it runs without one. */
  readonly limit_bytes: number | null;
  /** When it was measured. */
  readonly measured_at: string;
}

/** A registered worker. */
export interface StatusWorkerOut {
  /** The worker's id. */
  readonly id: string;
  /** Its host name. */
  readonly host: string;
  /** The Lakana version it runs. */
  readonly version: string;
  /** When it started. */
  readonly started_at: string;
  /** Its last heartbeat. */
  readonly heartbeat_at: string;
  /** Whether its heartbeat is younger than the lease. */
  readonly status: "alive" | "lost";
  /** The work it takes: everything, ad-hoc queries, or deploys and pipeline tasks. */
  readonly takes: "all" | "interactive" | "pipeline";
  /** Its oldest running query, if any. */
  readonly query_run_id: string | null;
  /** Its oldest running deploy, if any. */
  readonly deploy_run_id: string | null;
  /** Its oldest running task, if any. */
  readonly task_run_id: string | null;
  /** The memory it last reported; null before it reports any. */
  readonly memory?: WorkerMemoryOut | null;
  /** True for a lost worker that a restart on its host replaced. */
  readonly replaced: boolean;
}

/** Which server holds the scheduler, and how long its queues are. */
export interface SchedulerOut {
  /** The server that holds it; null when none does. */
  readonly holder: string | null;
  /** Its last heartbeat; null when none holds it. */
  readonly heartbeat_at: string | null;
  /** The runs not yet finished. */
  readonly active_runs: number;
  /** The tasks waiting for what they depend on. */
  readonly pending: number;
  /** The tasks ready for a worker. */
  readonly ready: number;
  /** The tasks a worker runs. */
  readonly running: number;
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

/**
 * Reads the scheduler; a background request, since Status polls it. Only those who see the
 * status's detail may.
 *
 * @returns Its holder and queues.
 */
export async function getScheduler(): Promise<SchedulerOut> {
  return api.request<SchedulerOut>("GET", "/api/v1/scheduler", undefined, { background: true });
}
