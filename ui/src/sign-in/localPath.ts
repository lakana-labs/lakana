/**
 * Whether a path the page kept stays inside the interface.
 *
 * @packageDocumentation
 */

/**
 * Whether a path stays inside the interface: `/` followed by anything but a second `/` or a
 * `\`, which a browser reads as another host (`//evil.example/x`, `/\evil.example/x`).
 *
 * @param path - The path.
 * @returns True for a local path.
 */
export function isLocalPath(path: unknown): path is string {
  return typeof path === "string" && /^\/(?![/\\])/.test(path);
}
