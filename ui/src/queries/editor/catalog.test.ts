import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TableListPageOut, TableTreeOut } from "../../api/catalog";
import { ApiError } from "../../api/client";
import { fakeServer } from "../../test/server";
import type { TableOut, TableRef } from "../../api/table";
import { item, TREE } from "../../test/catalog";
import { TABLE } from "../../test/table";

vi.mock("../../api/catalog", () => ({ getTableTree: vi.fn(), listCompletionTables: vi.fn() }));
vi.mock("../../api/table", () => ({ getTable: vi.fn() }));

// The module and its mocked calls, loaded again for each test: the cache lives for the page.
let catalog: typeof import("./catalog");
let client: typeof import("../../api/client");
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
  client = await import("../../api/client");
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
    getTable.mockRejectedValue(
      new client.ApiError(403, "Forbidden", "You may not read this table."),
    );
    const prod = catalog.catalogCache("prod");

    await expect(prod.columns("gold", "station_ranking")).resolves.toBeNull();
    // A refusal is kept like an answer: no second read while it is fresh.
    await expect(prod.columns("gold", "station_ranking")).resolves.toBeNull();
    expect(getTable).toHaveBeenCalledTimes(1);
  });
  it.each([
    [
      "a 404",
      (): Error => new client.ApiError(404, "NotFound", "There is no table station_ranking."),
    ],
    ["a 400", (): Error => new client.ApiError(400, "Invalid", "The name is not valid.")],
  ])("keeps %s like a 403: another read would answer the same", async (_, refused) => {
    const refusal = refused();
    getTableTree.mockRejectedValue(refusal);
    listCompletionTables.mockRejectedValue(refusal);
    getTable.mockRejectedValue(refusal);
    const prod = catalog.catalogCache("prod");

    for (let round = 0; round < 2; round += 1) {
      expect(await prod.namespaces()).toEqual([]);
      expect(await prod.tables("gold", "")).toEqual([]);
      expect(await prod.columns("gold", "station_ranking")).toBeNull();
    }
    expect(getTableTree).toHaveBeenCalledTimes(1);
    expect(listCompletionTables).toHaveBeenCalledTimes(1);
    expect(getTable).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a 503", (): Error => new client.ApiError(503, "Busy", "The server is busy.")],
    ["a 500", (): Error => new client.ApiError(500, "InternalError", "Something went wrong.")],
    [
      "no answer",
      (): Error => new client.ApiError(0, "NetworkError", "The server could not be reached."),
    ],
    [
      "a 429",
      (): Error => new client.ApiError(429, "TooManyRequests", "Too many requests; wait a moment."),
    ],
    ["a 408", (): Error => new client.ApiError(408, "Timeout", "The request took too long.")],
    ["an error that is not the server's", (): Error => new TypeError("Failed to fetch")],
  ])("holds %s for 15 s, then reads again and keeps the answer", async (_, failed) => {
    let now = 1_000_000;
    const failure = failed();
    const secret = { ...TABLE, name: "station_ranking" };
    getTableTree.mockRejectedValueOnce(failure);
    listCompletionTables
      .mockResolvedValueOnce(pageOf(["daily_sales"]))
      .mockRejectedValueOnce(failure)
      .mockResolvedValue(pageOf(["station_ranking"], null));
    getTable.mockRejectedValueOnce(failure).mockResolvedValue(secret);
    const prod = catalog.catalogCache("prod", () => now);

    expect(await prod.namespaces()).toEqual([]);
    // What was read before the failure answers this time.
    expect(await prod.tables("gold", "")).toEqual(["daily_sales"]);
    expect(await prod.columns("gold", "station_ranking")).toBeNull();

    // Asked again within the hold: the failure answers, and nothing is sent.
    now += catalog.FAILURE_HOLD_MS - 1;
    expect(await prod.namespaces()).toEqual([]);
    expect(await prod.tables("gold", "")).toEqual(["daily_sales"]);
    expect(await prod.columns("gold", "station_ranking")).toBeNull();
    expect(getTableTree).toHaveBeenCalledTimes(1);
    expect(listCompletionTables).toHaveBeenCalledTimes(2);
    expect(getTable).toHaveBeenCalledTimes(1);

    now += 1;
    expect(await prod.namespaces()).toEqual(["bronze", "silver", "gold", "gold.sales"]);
    expect(await prod.tables("gold", "")).toEqual(["station_ranking"]);
    expect((await prod.columns("gold", "station_ranking"))?.[0]?.name).toBe("sale_id");
    expect(getTableTree).toHaveBeenCalledTimes(2);
    expect(listCompletionTables).toHaveBeenCalledTimes(3);
    expect(getTable).toHaveBeenCalledTimes(2);

    // Now kept like any answer: nothing is sent while it is fresh.
    now += catalog.FAILURE_HOLD_MS;
    await prod.namespaces();
    await prod.tables("gold", "");
    await prod.columns("gold", "station_ranking");
    expect(getTableTree).toHaveBeenCalledTimes(2);
    expect(listCompletionTables).toHaveBeenCalledTimes(3);
    expect(getTable).toHaveBeenCalledTimes(2);
    expect(catalog.FAILURE_HOLD_MS).toBe(15_000);
  });

  it("sends one read per table while the server is down, however often it is asked", async () => {
    let now = 1_000_000;
    getTable.mockRejectedValue(new client.ApiError(0, "NetworkError", "No answer."));
    const prod = catalog.catalogCache("prod", () => now);

    // Twenty keystrokes over ten seconds, each asking for the columns of two tables.
    for (let key = 0; key < 20; key += 1) {
      await Promise.all([prod.columns("gold", "a"), prod.columns("gold", "b")]);
      now += 500;
    }
    expect(getTable).toHaveBeenCalledTimes(2);

    now += catalog.FAILURE_HOLD_MS;
    await Promise.all([prod.columns("gold", "a"), prod.columns("gold", "b")]);
    expect(getTable).toHaveBeenCalledTimes(4);
  });

  it("a failure that answers after a newer read was asked holds only itself", async () => {
    let now = 1_000_000;
    const failures: (() => void)[] = [];
    const failLater = <T>(): Promise<T> =>
      new Promise<T>((_, reject) => {
        failures.push(() => {
          reject(new client.ApiError(503, "Busy", "The server is busy."));
        });
      });
    getTableTree.mockImplementationOnce(failLater);
    listCompletionTables.mockImplementationOnce(failLater);
    getTable.mockImplementationOnce(failLater).mockResolvedValue(TABLE);
    listCompletionTables.mockResolvedValue(pageOf(["station_ranking"], null));
    const prod = catalog.catalogCache("prod", () => now);

    // The tree first: the table names wait for it.
    const oldTree = prod.namespaces();
    await vi.waitFor(() => {
      expect(failures).toHaveLength(1);
    });
    now += catalog.STALE_MS;
    expect(await prod.namespaces()).toEqual(["bronze", "silver", "gold", "gold.sales"]);
    failures[0]?.();
    expect(await oldTree).toEqual([]);

    const old = [prod.tables("gold", ""), prod.columns("gold", "fact_sales")];
    await vi.waitFor(() => {
      expect(failures).toHaveLength(3);
    });
    // The first reads are stale before they answer, so the next asks read again.
    now += catalog.STALE_MS;
    expect(await prod.tables("gold", "")).toEqual(["station_ranking"]);
    expect((await prod.columns("gold", "fact_sales"))?.[0]?.name).toBe("sale_id");
    failures[1]?.();
    failures[2]?.();
    await Promise.all(old);

    // The newer reads are still kept.
    await prod.namespaces();
    await prod.tables("gold", "");
    await prod.columns("gold", "fact_sales");
    expect(getTableTree).toHaveBeenCalledTimes(3);
    expect(listCompletionTables).toHaveBeenCalledTimes(2);
    expect(getTable).toHaveBeenCalledTimes(2);
  });

  it("holds a failed search for a while, then searches again", async () => {
    vi.useFakeTimers();
    getTableTree.mockResolvedValue(treeHolding(5_000));
    listCompletionTables.mockImplementation((_environment, q) =>
      q === ""
        ? Promise.resolve(pageOf([], null))
        : Promise.reject(new client.ApiError(503, "Busy", "")),
    );
    const prod = catalog.catalogCache("prod");
    const ask = async (): Promise<readonly string[]> => {
      const asked = prod.tables("gold", "sta");
      await vi.advanceTimersByTimeAsync(catalog.SEARCH_QUIET_MS);
      return asked;
    };

    expect(await ask()).toEqual([]);
    listCompletionTables.mockResolvedValue(pageOf(["station_ranking"], null));
    // Within the hold the failure answers at once, without a search.
    expect(await prod.tables("gold", "sta")).toEqual([]);
    await vi.advanceTimersByTimeAsync(catalog.FAILURE_HOLD_MS);
    expect(await ask()).toEqual(["station_ranking"]);
    expect(await prod.tables("gold", "sta")).toEqual(["station_ranking"]);
    expect(listCompletionTables.mock.calls.filter((call) => call[1] === "sta")).toHaveLength(2);
  });

  it.each([
    [
      "a sign-out",
      (): Promise<void> => {
        client.endSession();
        return Promise.resolve();
      },
    ],
    [
      "a 401",
      async (): Promise<void> => {
        fakeServer({ "GET /api/v1/whoami": { status: 401, body: { detail: "Sign in again." } } });
        await client.send("GET", "/api/v1/whoami").catch(() => undefined);
      },
    ],
  ])(
    "forgets the first person's catalog after %s, so the next person reads their own",
    async (_, end) => {
      const secret = { ...TABLE, name: "payroll" };
      getTable.mockResolvedValue(secret);
      listCompletionTables.mockResolvedValue(pageOf(["payroll"], null));
      const first = catalog.catalogCache("prod");
      expect(await first.namespaces()).toContain("gold.sales");
      expect(await first.tables("gold", "")).toEqual(["payroll"]);
      expect((await first.columns("gold", "payroll"))?.[0]?.name).toBe("sale_id");

      await end();
      getTableTree.mockResolvedValue({ count: 0, environments: [] });
      listCompletionTables.mockResolvedValue(pageOf([], null));
      getTable.mockRejectedValue(new ApiError(403, "Forbidden", "You may not read this table."));
      const next = catalog.catalogCache("prod");

      expect(next).not.toBe(first);
      expect(await next.namespaces()).toEqual([]);
      expect(await next.tables("gold", "")).toEqual([]);
      expect(await next.columns("gold", "payroll")).toBeNull();
      expect(getTableTree).toHaveBeenCalledTimes(2);
      expect(listCompletionTables).toHaveBeenCalledTimes(2);
      expect(getTable).toHaveBeenCalledTimes(2);
    },
  );
});
