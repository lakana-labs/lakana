/**
 * Profile pictures: read as object URLs, set and removed.
 *
 * @remarks
 * A picture is read through {@link api}.blob, with the session's CSRF token, and shown from a
 * `blob:` URL (the policy's `img-src 'self' blob:`). URLs are kept per name and etag and
 * counted: every place that shows a picture holds it ({@link pictureUrl}) and lets it go
 * ({@link releasePicture}), so a list of people reads each picture once and two places showing
 * two etags of one person never revoke each other's URL. A URL nothing shows is revoked once a
 * newer etag of that person is asked for or set, once the picture is removed, and at the end of
 * the session; the newest stays for the next place that shows the person. What this tab set or
 * removed overrides what the server answers for that name until the session ends, so a change
 * made on another device after it shows here only after signing in again.
 * @packageDocumentation
 */
import { api, onUnauthorized } from "./client";

/** The largest picture the server takes: 5 MB, as `PICTURE_MAX_BYTES` on the server. */
export const PICTURE_MAX_BYTES = 5 * 1024 * 1024;

/** The media types the server takes. */
export const PICTURE_TYPES: readonly string[] = ["image/jpeg", "image/png", "image/webp"];

/** One picture of one person: its object URL, once read, and how many places show it. */
interface Cached {
  /** The person's principal name. */
  readonly name: string;
  /** The etag the URL shows. */
  readonly etag: string;
  /** The object URL, or the read under way. */
  readonly url: Promise<string>;
  /** How many places show it now. */
  refs: number;
}

const cache = new Map<string, Cached>();
const newest = new Map<string, string | null>();
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
 * The cache key of one picture.
 *
 * @param name - The principal name.
 * @param etag - The etag.
 * @returns The key; no name or etag contains a NUL.
 */
function keyOf(name: string, etag: string): string {
  return `${name}\u0000${etag}`;
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
 * Revokes a person's pictures that nothing shows and that are not the newest etag.
 *
 * @param name - The principal name.
 */
function sweep(name: string): void {
  const keep = newest.get(name);
  for (const [key, entry] of cache) {
    if (entry.name === name && entry.refs === 0 && entry.etag !== keep) {
      revoke(entry);
      cache.delete(key);
    }
  }
}

/**
 * Holds a person's picture as an object URL, read once per name and etag; let it go with
 * {@link releasePicture} when it is no longer shown.
 *
 * @param name - The principal name.
 * @param etag - The picture's etag, from whoami or the principal.
 * @returns The object URL.
 * @throws {ApiError} when the picture could not be read; the next call tries again.
 */
export function pictureUrl(name: string, etag: string): Promise<string> {
  newest.set(name, etag);
  const key = keyOf(name, etag);
  let entry = cache.get(key);
  if (entry === undefined) {
    const url = api.blob(picturePath(name)).then((blob) => URL.createObjectURL(blob));
    const created: Cached = { name, etag, url, refs: 0 };
    cache.set(key, created);
    url.catch(() => {
      if (cache.get(key) === created) {
        cache.delete(key);
      }
    });
    entry = created;
  }
  entry.refs += 1;
  sweep(name);
  return entry.url;
}

/**
 * Lets go of a picture a place no longer shows; revoked when nothing shows it and a newer one
 * is known.
 *
 * @param name - The principal name.
 * @param etag - The etag the place held.
 */
export function releasePicture(name: string, etag: string): void {
  const entry = cache.get(keyOf(name, etag));
  if (entry === undefined) {
    return;
  }
  entry.refs -= 1;
  sweep(name);
}

/**
 * Records what this tab changed and tells every place that shows the person.
 *
 * @param name - The principal name.
 * @param etag - The new etag, or null without a picture.
 */
function noteChange(name: string, etag: string | null): void {
  changed.set(name, etag);
  newest.set(name, etag);
  sweep(name);
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
  newest.clear();
  changed.clear();
}

onUnauthorized(forgetPictures);
