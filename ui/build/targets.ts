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
  const oldest = new Map<string, string>();
  for (const entry of browserslist([...queries])) {
    const [name = "", versions = ""] = entry.split(" ");
    const [first = ""] = versions.split("-");
    const known = oldest.get(name);
    if (known === undefined || Number.parseFloat(first) < Number.parseFloat(known)) {
      oldest.set(name, first);
    }
  }
  return [...oldest].map(([name, version]) => `${name}${version}`);
}
