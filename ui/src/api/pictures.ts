/**
 * Profile pictures: read as object URLs, set and removed.
 *
 * @remarks
 * A picture is read through {@link api}.blob, with the session's CSRF token, and shown from a
 * `blob:` URL (the policy's `img-src 'self' blob:`). Each name keeps one URL, for its current
 * etag, so a list of people reads each picture once; the URL is revoked when the name's etag
 * changes, when the picture is removed, and when the session ends. What this tab set or removed
 * is remembered per name, so every place that shows the person follows at once.
 * @packageDocumentation
 */
import { api, onUnauthorized } from "./client";

/** The largest picture the server takes: 5 MB, as `PICTURE_MAX_BYTES` on the server. */
export const PICTURE_MAX_BYTES = 5 * 1024 * 1024;

/** The media types the server takes. */
export const PICTURE_TYPES: readonly string[] = ["image/jpeg", "image/png", "image/webp"];

/** One name's picture: its etag and its object URL, once read. */
interface Cached {
  /** The etag the URL shows. */
  readonly etag: string;
  /** The object URL, or the read under way. */
  readonly url: Promise<string>;
}

const cache = new Map<string, Cached>();
const changed = new Map<string, string | null>();
const listeners = new Set<() => void>();

/**
 * The path of a person's picture.
 *
 * @param name - The principal name.
 * @returns The path.
 */
function picturePath(name: string): string {
  return `/api/v1/principals/${encodeURIComponent(name)}/picture`;
}

/**
 * Revokes a cached URL once it is known; a read that failed has none.
 *
 * @param entry - The cached picture.
 */
function revoke(entry: Cached): void {
  entry.url.then(
    (url) => {
      URL.revokeObjectURL(url);
    },
    () => undefined,
  );
}

/**
 * Reads a person's picture into an object URL, once per name and etag.
 *
 * @param name - The principal name.
 * @param etag - The picture's etag, from whoami or the principal.
 * @returns The object URL.
 * @throws {ApiError} when the picture could not be read; the next call tries again.
 */
export function pictureUrl(name: string, etag: string): Promise<string> {
  const known = cache.get(name);
  if (known?.etag === etag) {
    return known.url;
  }
  if (known !== undefined) {
    revoke(known);
  }
  const url = api.blob(picturePath(name)).then((blob) => URL.createObjectURL(blob));
  const entry: Cached = { etag, url };
  cache.set(name, entry);
  url.catch(() => {
    if (cache.get(name) === entry) {
      cache.delete(name);
    }
  });
  return url;
}

/**
 * Records what this tab changed and tells every place that shows the person.
 *
 * @param name - The principal name.
 * @param etag - The new etag, or null without a picture.
 */
function noteChange(name: string, etag: string | null): void {
  changed.set(name, etag);
  const known = cache.get(name);
  if (etag === null && known !== undefined) {
    revoke(known);
    cache.delete(name);
  }
  for (const listener of listeners) {
    listener();
  }
}

/**
 * Sets one's own picture: the server keeps a square cut, without the file's metadata.
 *
 * @param name - One's own principal name.
 * @param file - A JPEG, PNG or WebP image of at most {@link PICTURE_MAX_BYTES}.
 * @returns The new picture's etag.
 * @throws {ApiError} 422 for a file the server cannot read, 503 while it is busy.
 */
export async function setPicture(name: string, file: File): Promise<string> {
  const { etag } = await api.upload<{ etag: string }>("PUT", picturePath(name), file, file.type);
  noteChange(name, etag);
  return etag;
}

/**
 * Removes a picture: one's own, or, for an administrator, anyone's.
 *
 * @param name - The principal name.
 */
export async function removePicture(name: string): Promise<void> {
  await api.request("DELETE", picturePath(name));
  noteChange(name, null);
}

/**
 * The etag this tab last set for a person.
 *
 * @param name - The principal name.
 * @returns The etag, null when removed here, undefined when unchanged here.
 */
export function changedPicture(name: string): string | null | undefined {
  return changed.get(name);
}

/**
 * Listens for pictures set or removed in this tab.
 *
 * @param listener - Called after each change.
 * @returns A function that stops listening.
 */
export function watchPictures(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Revokes every object URL and forgets every change: at sign-out, and when a session ends. */
export function forgetPictures(): void {
  for (const entry of cache.values()) {
    revoke(entry);
  }
  cache.clear();
  changed.clear();
}

onUnauthorized(forgetPictures);
