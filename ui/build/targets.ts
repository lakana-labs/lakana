/**
 * The build targets of the interface, from the browser list in `package.json`.
 *
 * @packageDocumentation
 */
import browserslist from "browserslist";

/**
 * Turn browserslist queries into the target names Vite and Lightning CSS understand.
 *
 * @remarks
 * browserslist answers `chrome 141` or, for a version range, `safari 18.5-18.7`; Vite wants
 * `chrome141` and `safari18.5`, and one minimum per browser. Each browser maps to the oldest
 * version the queries select (a range to its first version), so the build works on all of them.
 * @param queries - browserslist queries, such as `last 2 Chrome versions` or `chrome 141`.
 * @returns One target per browser, in the order browserslist names the browsers.
 */
export function browserTargets(queries: readonly string[]): string[] {
  return oldestVersions(browserslist([...queries]));
}

/**
 * Keep the oldest version of each browser among browserslist entries, as build targets.
 *
 * @param entries - browserslist answers, such as `chrome 141` or `safari 18.5-18.7`.
 * @returns One target per browser, such as `chrome141`, in the order the entries name them.
 */
export function oldestVersions(entries: readonly string[]): string[] {
  const oldest = new Map<string, string>();
  for (const entry of entries) {
    const [name = "", versions = ""] = entry.split(" ");
    const [first = ""] = versions.split("-");
    const known = oldest.get(name);
    if (known === undefined || compareVersions(first, known) < 0) {
      oldest.set(name, first);
    }
  }
  return [...oldest].map(([name, version]) => `${name}${version}`);
}

/**
 * Compare two dotted versions part by part, so that `18.9` comes before `18.10`.
 *
 * @param left - a version such as `18.9`.
 * @param right - a version such as `18.10`.
 * @returns A negative number when `left` is older, positive when newer, zero when equal.
 */
function compareVersions(left: string, right: string): number {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const difference = (a[i] ?? 0) - (b[i] ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
  return 0;
}
