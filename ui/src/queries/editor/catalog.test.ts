import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TableListPageOut, TableTreeOut } from "../../api/catalog";
import { ApiError } from "../../api/client";
import type { TableOut, TableRef } from "../../api/table";
import { item, TREE } from "../../test/catalog";
import { TABLE } from "../../test/table";

vi.mock("../../api/catalog", () => ({ getTableTree: vi.fn(), listCompletionTables: vi.fn() }));
vi.mock("../../api/table", () => ({ getTable: vi.fn() }));

// The module and its mocked calls, loaded again for each test: the cache lives for the page.
let catalog: typeof import("./catalog");
let getTableTree: ReturnType<typeof vi.fn<() => Promise<TableTreeOut>>>;
let listCompletionTables: ReturnType<
  typeof vi.fn<(environment: string, q: string, cursor: string | null) => Promise<TableListPageOut>>
>;
let getTable: ReturnType<typeof vi.fn<(ref: TableRef) => Promise<TableOut>>>;

beforeEach(async () => {
  vi.resetModules();
  const api = await import("../../api/catalog");
  const table = await import("../../api/table");
  getTableTree = vi.mocked(api.getTableTree);
  listCompletionTables = vi.mocked(api.listCompletionTables);
  getTable = vi.mocked(table.getTable);
  catalog = await import("./catalog");
  getTableTree.mockResolvedValue(TREE);
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * A page of tables in `prod.gold`, named by their numbers, with a next page.
 *
 * @param names - The tables' names.
 * @param next - The next page's cursor, or null on the last.
 * @returns The page.
 */
function pageOf(names: readonly string[], next: string | null = "more"): TableListPageOut {
  return { items: names.map((name) => item({ name })), next_cursor: next, capped: false };
}

/**
 * The tree with `prod` holding a number of tables.
 *
 * @param count - How many tables `prod` holds.
 * @returns The tree.
 */
function treeHolding(count: number): TableTreeOut {
  return {
    ...TREE,
    environments: TREE.environments.map((environment) =>
      environment.name === "prod" ? { ...environment, count } : environment,
    ),
  };
}

describe("catalogCache", () => {
  it("reads the tree once and table names in at most five pages", async () => {
    let page = 0;
    listCompletionTables.mockImplementation(() => {
      page += 1;
      return Promise.resolve(pageOf([`table_${String(page)}`]));
    });
    const prod = catalog.catalogCache("prod");

    expect(await prod.namespaces()).toEqual(["bronze", "silver", "gold", "gold.sales"]);
    expect(await catalog.catalogCache("dev").namespaces()).toEqual(["bronze", "gold"]);
    expect(await catalog.catalogCache("test").namespaces()).toEqual([]);
    expect(catalog.catalogCache("prod")).toBe(prod);
    expect(getTableTree).toHaveBeenCalledTimes(1);

    expect(await prod.tables("gold", "")).toEqual([
      "table_1",
      "table_2",
      "table_3",
      "table_4",
      "table_5",
    ]);
    expect(await prod.tables("silver", "st")).toEqual([]);
    expect(listCompletionTables).toHaveBeenCalledTimes(catalog.TABLE_PAGES);
    expect(listCompletionTables.mock.calls.map((call) => call[2])).toEqual([
      null,
      "more",
      "more",
      "more",
      "more",
    ]);
    expect(listCompletionTables).toHaveBeenCalledWith("prod", "", null);
  });

  it("stops at the last page and keeps what it read when a page fails", async () => {
    listCompletionTables.mockResolvedValueOnce(pageOf(["station_ranking"], null));
    expect(await catalog.catalogCache("prod").tables("gold", "")).toEqual(["station_ranking"]);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);

    listCompletionTables
      .mockResolvedValueOnce(pageOf(["daily_sales"]))
      .mockRejectedValueOnce(new ApiError(503, "Busy", "The server is busy."));
    expect(await catalog.catalogCache("dev").tables("gold", "")).toEqual(["daily_sales"]);
  });

  it("answers without tables or namespaces when the tree cannot be read", async () => {
    getTableTree.mockRejectedValue(new ApiError(503, "Busy", "The server is busy."));
    listCompletionTables.mockResolvedValue(pageOf(["station_ranking"], null));
    const prod = catalog.catalogCache("prod");

    expect(await prod.namespaces()).toEqual([]);
    // Without the count, the first pages are all there is: no search.
    expect(await prod.tables("gold", "sta")).toEqual(["station_ranking"]);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);
  });

  it("searches by prefix after two characters and 300 ms when the environment holds more than 1,000 tables", async () => {
    vi.useFakeTimers();
    getTableTree.mockResolvedValue(treeHolding(1_001));
    listCompletionTables.mockImplementation((_environment, q) =>
      Promise.resolve(
        q === "" ? pageOf(["daily_sales"], null) : pageOf([`${q}tion_ranking`], null),
      ),
    );
    const prod = catalog.catalogCache("prod");

    // One character: the first pages, no search.
    expect(await prod.tables("gold", "s")).toEqual(["daily_sales"]);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);

    // Two words typed within 300 ms: only the last is asked, once it has been quiet for 300 ms.
    const first = prod.tables("gold", "st");
    await vi.advanceTimersByTimeAsync(100);
    const second = prod.tables("gold", "sta");
    await vi.advanceTimersByTimeAsync(catalog.SEARCH_QUIET_MS - 1);
    // The word typed over answers from the first pages, without a search.
    expect(await first).toEqual(["daily_sales"]);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    expect(await second).toEqual(["station_ranking"]);
    expect(listCompletionTables).toHaveBeenCalledTimes(2);
    expect(listCompletionTables).toHaveBeenLastCalledWith("prod", "sta", null);
    expect(catalog.SEARCH_AFTER_CHARS).toBe(2);
  });

  it("answers the same prefix from the cache", async () => {
    vi.useFakeTimers();
    getTableTree.mockResolvedValue(treeHolding(5_000));
    listCompletionTables.mockImplementation((_environment, q) =>
      Promise.resolve(pageOf(q === "" ? [] : ["station_ranking"], null)),
    );
    const prod = catalog.catalogCache("prod");
    const asked = prod.tables("gold", "sta");
    await vi.advanceTimersByTimeAsync(catalog.SEARCH_QUIET_MS);
    expect(await asked).toEqual(["station_ranking"]);
    const calls = listCompletionTables.mock.calls.length;

    // At once, without waiting: the cache answers.
    expect(await prod.tables("gold", "sta")).toEqual(["station_ranking"]);
    expect(await prod.tables("silver", "sta")).toEqual([]);
    expect(listCompletionTables).toHaveBeenCalledTimes(calls);
  });

  it("reads a table's columns once, at most four at a time", async () => {
    const answers: (() => void)[] = [];
    getTable.mockImplementation(
      (ref) =>
        new Promise((resolve) => {
          answers.push(() => {
            resolve({ ...TABLE, name: ref.name });
          });
        }),
    );
    const prod = catalog.catalogCache("prod");
    const names = ["a", "b", "c", "d", "e", "f"];

    const reads = names.map((name) => prod.columns("gold", name));
    const again = prod.columns("gold", "a");
    await Promise.resolve();
    expect(getTable).toHaveBeenCalledTimes(catalog.COLUMN_READS_IN_FLIGHT);
    expect(getTable).toHaveBeenCalledWith({ environment: "prod", namespace: "gold", name: "a" });

    answers.shift()?.();
    await vi.waitFor(() => {
      expect(getTable).toHaveBeenCalledTimes(5);
    });
    answers.shift()?.();
    await vi.waitFor(() => {
      expect(getTable).toHaveBeenCalledTimes(names.length);
    });
    for (const answer of answers.splice(0)) {
      answer();
    }

    const columns = await Promise.all(reads);
    expect(columns.map((list) => list?.map((column) => column.name)[0])).toEqual(
      names.map(() => "sale_id"),
    );
    expect(await again).toBe(columns[0]);
    expect(getTable).toHaveBeenCalledTimes(names.length);
  });

  it("reads again after five minutes", async () => {
    let now = 1_000_000;
    const clock = (): number => now;
    getTable.mockResolvedValue(TABLE);
    listCompletionTables.mockResolvedValue(pageOf(["station_ranking"], null));
    const prod = catalog.catalogCache("prod", clock);
    await prod.namespaces();
    await prod.tables("gold", "");
    await prod.columns("gold.sales", "fact_sales");

    now += catalog.STALE_MS - 1;
    await prod.namespaces();
    await prod.tables("gold", "");
    await prod.columns("gold.sales", "fact_sales");
    expect(getTableTree).toHaveBeenCalledTimes(1);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);
    expect(getTable).toHaveBeenCalledTimes(1);

    now += 1;
    await prod.namespaces();
    await prod.tables("gold", "");
    await prod.columns("gold.sales", "fact_sales");
    expect(getTableTree).toHaveBeenCalledTimes(2);
    expect(listCompletionTables).toHaveBeenCalledTimes(2);
    expect(getTable).toHaveBeenCalledTimes(2);
    expect(catalog.STALE_MS).toBe(300_000);
  });

  it("a 403 on a table's read gives no columns and no error", async () => {
    getTable.mockRejectedValue(new ApiError(403, "Forbidden", "You may not read this table."));
    const prod = catalog.catalogCache("prod");

    await expect(prod.columns("gold", "station_ranking")).resolves.toBeNull();
    // The answer is kept like any other: no second read while it is fresh.
    await expect(prod.columns("gold", "station_ranking")).resolves.toBeNull();
    expect(getTable).toHaveBeenCalledTimes(1);
  });
});
