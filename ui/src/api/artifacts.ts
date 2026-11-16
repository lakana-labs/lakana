/**
 * The calls of an artifact's page: the artifact, its versions and their files, deleting a version,
 * the reader's open uploads and withdrawing one, and downloading one file, the same routes
 * `lakana artifact versions list|delete`, `artifact uploads list|abort` and `artifact download`
 * use. The artifact's drop is `manage.ts`'s `dropArtifact`.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import {
  artifactApiPath,
  breakGlassQuery,
  environmentPath,
  pageQuery,
  withQuery,
  type CursorPage,
} from "./manage";
import type { DefinedByOut, TableRef } from "./table";

/** An artifact, as `GET …/artifacts/{name}` answers it. */
export interface ArtifactOut {
  /** The artifact's name. */
  readonly name: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The artifact's id. */
  readonly uuid: string;
  /** Always `ARTIFACT`. */
  readonly kind: "ARTIFACT";
  /** The highest live version; null while it has none. */
  readonly current_version: number | null;
  /** How many versions it has had, deleted ones included. */
  readonly versions: number;
  /** The summed size of its live versions. */
  readonly size_bytes: number;
  /** When it was created. */
  readonly created_at: string;
  /** The deployed project file that declares it; null when none does. */
  readonly defined_by: DefinedByOut | null;
  /** How many live versions the declaration keeps; null keeps all. */
  readonly keep_versions: number | null;
  /** `drop` when the caller's rights allow it; a git-owned artifact is still refused. */
  readonly actions: readonly "drop"[];
}

/** One file of an artifact version. */
export interface ArtifactFileOut {
  /** Its path inside the artifact, `/`-separated. */
  readonly path: string;
  /** Its size in bytes. */
  readonly size: number;
  /** The SHA-256 of its contents, as lower-case hex. */
  readonly sha256: string;
}

/** One version of an artifact. */
export interface ArtifactVersionOut {
  /** The number; a number is never used again, so there can be gaps. */
  readonly version: number;
  /** The digest of the canonical manifest: the identity of the content. */
  readonly manifest_sha256: string;
  /** The summed size of its files. */
  readonly size_bytes: number;
  /** The number of its files. */
  readonly file_count: number;
  /** Whether it is a directory (otherwise one file). */
  readonly is_directory: boolean;
  /** The task run that committed it, if one did. */
  readonly task_run_id: string | null;
  /** That task run's run. */
  readonly run_id: string | null;
  /** The project version the run executed. */
  readonly project_version_id: string | null;
  /** The commit of that project version. */
  readonly commit_sha: string | null;
  /** Who committed it; null when unknown. */
  readonly created_by: string | null;
  /** When it was committed. */
  readonly created_at: string;
  /** When it was deleted; null while it is live. */
  readonly deleted_at: string | null;
  /** Who deleted it. */
  readonly deleted_by: string | null;
  /** Its files; empty in a listing and for a deleted version. */
  readonly files: readonly ArtifactFileOut[];
  /** The versions this commit pruned (`keep_versions`). */
  readonly deleted_versions: readonly number[];
  /** `delete` on a live version when the caller's rights allow it. */
  readonly actions: readonly "delete"[];
}

/** Files a delete left in storage, for one reason. */
export interface ArtifactKeptOut {
  /** Another live version has them, an open upload names them, or the periodic clean-up decides. */
  readonly reason: "other_version" | "open_upload" | "left_to_sweep";
  /** How many stored files. */
  readonly files: number;
  /** Their summed size. */
  readonly bytes: number;
  /** For `other_version`, the newest live version that has them; null otherwise or when hidden. */
  readonly version: number | null;
}

/** The answer to deleting a version: the version, and what happened to its files. */
export interface ArtifactVersionDeletedOut extends ArtifactVersionOut {
  /** Stored files this delete removed. */
  readonly freed_files: number;
  /** Their summed size. */
  readonly freed_bytes: number;
  /** Stored files that stayed. */
  readonly kept_files: number;
  /** Their summed size. */
  readonly kept_bytes: number;
  /** The files that stayed, by reason; empty when none did. */
  readonly kept: readonly ArtifactKeptOut[];
}

/** One open upload of the reader's. */
export interface ArtifactUploadOut {
  /** The upload's id, which the withdraw route takes. */
  readonly upload_id: string;
  /** The artifact it adds a version to, as `namespace.name`. */
  readonly artifact: string;
  /** When it was begun. */
  readonly created_at: string;
  /** When it stops being usable and frees its place. */
  readonly expires_at: string;
}

/** The largest file the browser downloads, 256 MiB; a larger one takes `lakana artifact download`. */
export const BROWSER_DOWNLOAD_LIMIT = 256 * 1024 ** 2;

/** A downloaded file whose size or SHA-256 differs from the version's manifest. */
export class DownloadMismatch extends Error {
  /** The file's path inside the artifact. */
  readonly path: string;

  /**
   * Builds the error.
   *
   * @param path - The file's path inside the artifact.
   */
  constructor(path: string) {
    super(`${path} did not arrive whole.`);
    this.name = "DownloadMismatch";
    this.path = path;
  }
}

/**
 * Reads an artifact.
 *
 * @param ref - The artifact.
 * @returns The artifact.
 * @throws {ApiError} for a refusal.
 */
export function getArtifact(ref: TableRef): Promise<ArtifactOut> {
  return api.request<ArtifactOut>("GET", artifactApiPath(ref));
}

/**
 * Lists an artifact's versions, newest first, deleted ones included, in one answer.
 *
 * @param ref - The artifact.
 * @returns The versions, without their files.
 * @throws {ApiError} for a refusal.
 */
export function listVersions(ref: TableRef): Promise<readonly ArtifactVersionOut[]> {
  return api.request<readonly ArtifactVersionOut[]>("GET", artifactApiPath(ref, "/versions"));
}

/**
 * Reads one version with its files.
 *
 * @param ref - The artifact.
 * @param version - The version's number.
 * @returns The version.
 * @throws {ApiError} for a refusal; 404 for a version that does not exist.
 */
export function getVersion(ref: TableRef, version: number): Promise<ArtifactVersionOut> {
  return api.request<ArtifactVersionOut>(
    "GET",
    artifactApiPath(ref, `/versions/${String(version)}`),
  );
}

/**
 * Deletes one version. From the browser the server asks for a passkey touch first, which
 * `api.request` runs.
 *
 * @param ref - The artifact.
 * @param version - The version's number.
 * @param breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns The deleted version, and what happened to its files.
 * @throws {ApiError} for a refusal; 409 for a version already deleted.
 */
export function deleteVersion(
  ref: TableRef,
  version: number,
  breakGlassReason: string | null,
): Promise<ArtifactVersionDeletedOut> {
  const path = artifactApiPath(ref, `/versions/${String(version)}`);
  return api.request<ArtifactVersionDeletedOut>(
    "DELETE",
    withQuery(path, breakGlassQuery(breakGlassReason)),
  );
}

/**
 * Lists the reader's own open uploads of one artifact, newest first.
 *
 * @param environment - The environment.
 * @param artifact - The artifact, as `namespace.name`.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export async function listUploads(
  environment: string,
  artifact: string,
  cursor: string | null,
): Promise<CursorPage<ArtifactUploadOut>> {
  const query = pageQuery(cursor, new URLSearchParams({ name: artifact }));
  const body = await api.request<{
    items: readonly ArtifactUploadOut[];
    next_cursor: string | null;
  }>("GET", withQuery(environmentPath(environment, "/artifacts/uploads"), query));
  return { items: body.items, nextCursor: body.next_cursor };
}

/**
 * Withdraws one of the reader's open uploads, which frees its place.
 *
 * @param ref - The artifact the upload adds a version to.
 * @param uploadId - The upload's id.
 * @returns Nothing.
 * @throws {ApiError} for a refusal; 409 for an upload that expired or was committed.
 */
export async function withdrawUpload(ref: TableRef, uploadId: string): Promise<void> {
  const path = artifactApiPath(ref, `/uploads/${encodeURIComponent(uploadId)}`);
  await api.request<undefined>("DELETE", path);
}

/**
 * The lower-case hex SHA-256 of a file.
 *
 * @param blob - The file.
 * @returns The digest.
 */
async function sha256Hex(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Downloads one file of a version and checks it against the manifest.
 *
 * @param ref - The artifact.
 * @param version - The version's number.
 * @param file - The file, as the version lists it.
 * @returns The file's bytes, the same size and SHA-256 as the manifest's.
 * @throws {DownloadMismatch} when the size or the SHA-256 differs.
 * @throws {ApiError} for a refusal; 404 for a version deleted meanwhile.
 */
export async function downloadFile(
  ref: TableRef,
  version: number,
  file: ArtifactFileOut,
): Promise<Blob> {
  const blob = await api.download(
    artifactApiPath(ref, `/versions/${String(version)}/blobs/${encodeURIComponent(file.sha256)}`),
  );
  if (blob.size !== file.size || (await sha256Hex(blob)) !== file.sha256) {
    throw new DownloadMismatch(file.path);
  }
  return blob;
}
