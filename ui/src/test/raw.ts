/**
 * What the server answers the listing of `raw/` with, for tests: a small lake with a free
 * folder, a folder claimed by a table the person may read, one claimed by a table they may not,
 * and a folder of two pages.
 *
 * @packageDocumentation
 */
import type { RawEntryOut } from "../api/raw";
import type { FakeRoute } from "./server";

/**
 * A file, as the server lists it.
 *
 * @param name - Its name.
 * @param size - Its size in bytes.
 * @param table - The claiming table: a name, null for one the person may not read, or undefined
 *   for a free file.
 * @returns The entry.
 */
export function rawFile(name: string, size: number, table?: string | null): RawEntryOut {
  return { name, kind: "file", size, claimed_by: table === undefined ? null : { table } };
}

/**
 * A folder, as the server lists it.
 *
 * @param name - Its name.
 * @param table - The claiming table, as for {@link rawFile}.
 * @returns The entry.
 */
export function rawFolder(name: string, table?: string | null): RawEntryOut {
  return { name, kind: "folder", size: null, claimed_by: table === undefined ? null : { table } };
}

/** Each folder of the lake, as its pages. */
export const LAKE: Readonly<Record<string, readonly (readonly RawEntryOut[])[]>> = {
  raw: [
    [
      rawFile("events.csv", 2048),
      rawFolder("feeds"),
      rawFolder("orders", "bronze.orders"),
      rawFolder("payments", null),
    ],
  ],
  "raw/feeds": [
    [
      rawFile("2026-11-01.CSV", 1200),
      rawFile("2026-11-02.csv", 1300),
      rawFile("2026-11-03.csv", 1250),
      rawFolder("archive"),
      rawFile("readme.txt", 10),
    ],
  ],
  "raw/feeds/archive": [[]],
  "raw/orders": [[rawFile("2026-11-01.csv", 980, "bronze.orders")]],
  "raw/stations": [
    [rawFile("berlin.parquet", 4096), rawFile("hamburg.parquet", 4100)],
    [rawFile("munich.json", 300)],
  ],
};

/**
 * The route of the listing in `prod`, answering from {@link LAKE}; a cursor `p<N>` reads page N.
 *
 * @param lake - The folders and their pages.
 * @returns The fake route table.
 */
export function rawRoutes(
  lake: Readonly<Record<string, readonly (readonly RawEntryOut[])[]>> = LAKE,
): Record<string, FakeRoute> {
  return {
    "GET /api/v1/environments/prod/raw": (request) => {
      const query = new URL(request.path, "http://localhost").searchParams;
      const path = query.get("path") ?? "raw";
      const pages = lake[path];
      if (pages === undefined) {
        return { status: 200, body: { path, items: [], next_cursor: null } };
      }
      const index = Number((query.get("cursor") ?? "p0").slice(1));
      return {
        status: 200,
        body: {
          path,
          items: pages[index],
          next_cursor: index + 1 < pages.length ? `p${String(index + 1)}` : null,
        },
      };
    },
  };
}
