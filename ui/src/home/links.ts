/**
 * Where Home's rows and links go: the interface's places for an object, the documentation site
 * and the release notes.
 *
 * @packageDocumentation
 */
import type { AttentionKind } from "../api";

/** The public documentation site; opening it is the person's own browsing. */
export const DOCS_URL = "https://lakana-labs.github.io/lakana/";

/** Where the release notes of each version are. */
const RELEASES_URL = "https://github.com/lakana-labs/lakana/releases/tag/";

/** A kind of object a row of Home opens. */
export type ObjectKind = "table" | "query" | "job" | "run" | "check" | "worker" | "recovery_link";

/**
 * The place each kind of object opens at, from its environment and reference. Until an area's
 * part brings its detail pages, an object opens the area's own page, which names the command to
 * use meanwhile; each later part changes its kinds' lines here to the detail page (part 3 tables, part 4 queries, part 5 jobs,
 * runs and checks, part 8 workers).
 */
const PLACES: Record<ObjectKind, (environment: string | null, ref: string) => string> = {
  table: () => "/catalog",
  query: () => "/queries",
  job: () => "/jobs",
  run: () => "/jobs",
  check: () => "/jobs",
  worker: () => "/settings/status",
  recovery_link: () => "/access/recovery-links",
};

/** The object each kind of Needs your attention row opens. */
export const ATTENTION_OBJECTS: Record<AttentionKind, ObjectKind> = {
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
 * @returns The interface path.
 */
export function objectPath(kind: ObjectKind, environment: string | null, ref: string): string {
  return PLACES[kind](environment, ref);
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
