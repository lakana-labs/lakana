/**
 * An environment's storage in a few words: its kind and its root.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { EnvironmentOut } from "../api/environments";

/** The kinds of storage the server opens, by the scheme of their root. */
const KINDS = new Set(["file", "s3", "abfs"]);

/**
 * The scheme of a storage root: the part before `://`.
 *
 * @param root - The root.
 * @returns The scheme, or the whole root when it has none.
 */
export function storageScheme(root: string): string {
  return root.split("://", 1)[0] as string;
}

/**
 * The storage's kind in a word and its root ("S3 · s3://corp-lake/dev"); the root alone when
 * Lakana opens no storage of its kind.
 *
 * @param t - The translation function.
 * @param environment - The environment.
 * @returns The summary.
 */
export function storageSummary(t: TFunction, environment: EnvironmentOut): string {
  const scheme = storageScheme(environment.storage_root);
  return KINDS.has(scheme)
    ? t("settings.environmentPages.storageSummary", {
        kind: t(`settings.environmentPages.kinds.${scheme}`),
        root: environment.storage_root,
      })
    : t("settings.environmentPages.storageUnknown", { root: environment.storage_root });
}
