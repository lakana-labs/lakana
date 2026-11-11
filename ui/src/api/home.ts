/**
 * Home's lists for the signed-in person: Needs your attention, Your work and Get started.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** What a row of Needs your attention is about. */
export type AttentionKind =
  | "run_failed"
  | "table_late"
  | "check_failed"
  | "query_failed"
  | "recovery_link_open"
  | "worker_lost";

/** One row of Needs your attention. */
interface AttentionItemOut {
  /** What happened. */
  readonly kind: AttentionKind;
  /** The environment's name; null for a recovery link and a worker. */
  readonly environment: string | null;
  /** The object's name, such as a job, a dotted table name or a worker's host. */
  readonly title: string;
  /** The object the row opens: a run's, query's, link's or worker's id, or a table name. */
  readonly ref: string;
  /** When it happened. */
  readonly at: string;
}

/** Needs your attention, as `GET /api/v1/home/attention` answers it. */
export interface AttentionOut {
  /** At most 50 rows, newest first. */
  readonly items: readonly AttentionItemOut[];
  /** Whether more rows were left out. */
  readonly truncated: boolean;
}

/** Where one of the person's repositories is deployed. */
interface WorkEnvironmentOut {
  /** The environment's name. */
  readonly environment: string;
  /** How the newest deploy there ended, or null when none is recorded. */
  readonly last_deploy_status: string | null;
  /** When that deploy ended, or was requested while it has not. */
  readonly last_deploy_at: string | null;
}

/** A repository the person created or deployed. */
export interface WorkRepositoryOut {
  /** The repository's name. */
  readonly name: string;
  /** The environments the person may read where it is deployed. */
  readonly environments: readonly WorkEnvironmentOut[];
}

/** A job of one of the person's repositories. */
interface WorkJobOut {
  /** The environment's name. */
  readonly environment: string;
  /** The repository's name. */
  readonly repository: string;
  /** The job's name. */
  readonly job: string;
  /** The state of its newest run, or null before the first. */
  readonly last_run_status: string | null;
  /** When that run was started. */
  readonly last_run_at: string | null;
}

/** Your work, as `GET /api/v1/home/work` answers it. */
export interface WorkOut {
  /** At most 20 repositories, the most recently deployed first. */
  readonly repositories: readonly WorkRepositoryOut[];
  /** At most 20 of their jobs, the most recently run first. */
  readonly jobs: readonly WorkJobOut[];
}

/** A step of Get started. */
export type GetStartedStepId =
  | "connect_repository"
  | "invite_team"
  | "add_passkey"
  | "deploy_project"
  | "deploy_project_local"
  | "ci_deploy"
  | "open_table"
  | "run_query";

/** One row of Get started. */
interface GetStartedStepOut {
  /** The step. */
  readonly id: GetStartedStepId;
  /** Whether the server's state shows it done. */
  readonly done: boolean;
}

/** Get started, as `GET /api/v1/home/get-started` answers it. */
export interface GetStartedOut {
  /** `admin`, `deployer`, `reader` or `none`. */
  readonly audience: "admin" | "deployer" | "reader" | "none";
  /** Whether the server runs in local mode. */
  readonly local: boolean;
  /** The rows in order; empty for `none`. */
  readonly steps: readonly GetStartedStepOut[];
  /** Whether the person hid the list. */
  readonly hidden: boolean;
  /** Whether every step is done. */
  readonly complete: boolean;
}

/**
 * Reads what needs the person's attention; a background request, since Home polls it.
 *
 * @returns The rows and whether more were left out.
 */
export async function getAttention(): Promise<AttentionOut> {
  return api.request<AttentionOut>("GET", "/api/v1/home/attention", undefined, {
    background: true,
  });
}

/**
 * Reads the person's repositories and their jobs.
 *
 * @returns The work; two empty lists for a person who may not deploy.
 */
export async function getWork(): Promise<WorkOut> {
  return api.request<WorkOut>("GET", "/api/v1/home/work");
}

/**
 * Reads the person's Get started list.
 *
 * @returns The steps and whether the list is hidden or complete.
 */
export async function getGetStarted(): Promise<GetStartedOut> {
  return api.request<GetStartedOut>("GET", "/api/v1/home/get-started");
}
