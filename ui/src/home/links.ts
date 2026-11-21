/**
 * Where Home's rows and links go: the interface's places for an object and the release
 * notes.
 *
 * @packageDocumentation
 */
import type { AttentionItemOut, AttentionKind } from "../api";
import { splitDotted, tablePath } from "../catalog/paths";
import { deploysPath, jobPath, jobRunPath, splitJobRef } from "../jobs/paths";
import { runPath } from "../queries/paths";

/** Where the release notes of each version are. */
const RELEASES_URL = "https://github.com/lakana-labs/lakana/releases/tag/";

/** A kind of object a row of Home opens. */
export type ObjectKind = "table" | "query" | "job" | "run" | "check" | "worker" | "recovery_link";

/**
 * The place each kind of object opens at, from its environment and reference; null for an
 * object that has no place (a table with no environment or no namespace in its name). A table
 * opens its page, and a query its run on the Queries screen (the Queries screen itself without
 * an environment); a job opens its page (none for a reference without a `repository/` part; the Jobs area
 * without an environment), and a run and a check the run's page (the Jobs area without an environment); until
 * an area's part brings its detail pages, the other kinds open the area's own page, which names
 * the command to use meanwhile; each later part changes its kinds' lines here to the detail page
 * (part 8 workers).
 */
const PLACES: Record<ObjectKind, (environment: string | null, ref: string) => string | null> = {
  table: (environment, ref) => {
    const place = environment === null ? null : splitDotted(ref);
    return environment === null || place === null
      ? null
      : tablePath(environment, place.namespace, place.name);
  },
  query: (environment, ref) => (environment === null ? "/queries" : runPath(environment, ref)),
  job: (environment, ref) => {
    if (environment === null) {
      return "/jobs";
    }
    const place = splitJobRef(ref);
    return place === null ? null : jobPath(environment, place.repository, place.job);
  },
  run: (environment, ref) => (environment === null ? "/jobs" : jobRunPath(environment, ref)),
  check: (environment, ref) => (environment === null ? "/jobs" : jobRunPath(environment, ref)),
  worker: () => "/settings/status",
  recovery_link: () => "/settings/sign-in/recovery-links",
};

/**
 * The object each kind of Needs your attention row opens; a failed deploy has no page of its
 * own and opens its repository's deploys instead (see {@link attentionPath}).
 */
const ATTENTION_OBJECTS: Record<Exclude<AttentionKind, "deploy_failed">, ObjectKind> = {
  run_failed: "run",
  table_late: "table",
  check_failed: "check",
  query_failed: "query",
  recovery_link_open: "recovery_link",
  worker_lost: "worker",
};

/**
 * The path that opens an object.
 *
 * @remarks
 * Every caller passes the object's environment and reference already (a dotted table name, a
 * query's, run's, link's or worker's id, `repository/job` for a job, the run's id for a failed
 * check), so that a detail page needs no change at the call sites; for now only the kind
 * decides (see {@link PLACES}).
 * @param kind - What the object is.
 * @param environment - Its environment; null for a worker and a recovery link.
 * @param ref - The object within its kind.
 * @returns The interface path; null when the object has no place, which the caller shows as
 *   plain text.
 */
export function objectPath(
  kind: ObjectKind,
  environment: string | null,
  ref: string,
): string | null {
  return PLACES[kind](environment, ref);
}

/**
 * The path a row of Needs your attention opens: a failed run's or check's run, a failed
 * deploy's repository on the Deploys tab of its environment (where the newest deploy, the
 * failed one, comes first with why it failed), or the object's place.
 *
 * @param item - The row.
 * @returns The interface path; null when the object has no place.
 */
export function attentionPath(item: AttentionItemOut): string | null {
  if (item.kind === "deploy_failed") {
    return deploysPath(item.environment, item.title);
  }
  return objectPath(ATTENTION_OBJECTS[item.kind], item.environment, item.ref);
}

/**
 * Whether a version is a release (`x.y.z`), not a development build.
 *
 * @param version - The version the server runs, such as `0.23.0` or `0.23.1.dev4+g8dc908d7`.
 * @returns True for a release.
 */
export function isReleaseVersion(version: string): boolean {
  return /^\d+\.\d+\.\d+$/.test(version);
}

/**
 * The release notes of a version.
 *
 * @param version - The version.
 * @returns The GitHub release's address; null for a development build, which has none.
 */
export function releaseNotesUrl(version: string): string | null {
  return isReleaseVersion(version) ? `${RELEASES_URL}v${version}` : null;
}
