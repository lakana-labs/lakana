import {
  type Completion,
  CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { EditorState } from "@codemirror/state";
import { describe, expect, it, vi } from "vitest";

import type { ColumnOut } from "../../api/table";
import { TABLE } from "../../test/table";
import type { CompletionCatalog } from "./catalog";
import { catalogCompletion } from "./completion";

const STATION_COLUMNS: readonly ColumnOut[] = [
  { ...(TABLE.schema[0] as ColumnOut), name: "station_id", type: "long" },
  {
    ...(TABLE.schema[1] as ColumnOut),
    name: "rank",
    type: "int",
    description: '<img src=x onerror="alert(1)"> Rank by rides\u202E',
  },
];

/**
 * A catalog of `prod`: `gold` with two tables, `gold.sales` with one, and `raw.weather`.
 *
 * @returns The catalog, each method a mock.
 */
function aCatalog(): CompletionCatalog {
  return {
    namespaces: vi.fn(() => Promise.resolve(["gold", "gold.sales", "raw.weather"])),
    tables: vi.fn((namespace: string) =>
      Promise.resolve(
        namespace === "gold"
          ? ["daily_sales", "station_ranking"]
          : namespace === "gold.sales"
            ? ["fact_sales"]
            : [],
      ),
    ),
    columns: vi.fn((namespace: string, table: string) =>
      Promise.resolve(
        namespace === "gold" && table === "station_ranking"
          ? STATION_COLUMNS
          : namespace === "gold.sales"
            ? TABLE.schema
            : null,
      ),
    ),
  };
}

/**
 * Asks the catalog's completion at the `|` in a text.
 *
 * @param text - The SQL with a `|` where the cursor is.
 * @param catalog - The catalog.
 * @param explicit - Whether the person asked (Ctrl Space).
 * @returns The completion's answer.
 */
async function complete(
  text: string,
  catalog: CompletionCatalog = aCatalog(),
  explicit = false,
): Promise<CompletionResult | null> {
  const pos = text.indexOf("|");
  const state = EditorState.create({ doc: text.replace("|", "") });
  return catalogCompletion(catalog)(new CompletionContext(state, pos, explicit));
}

/**
 * The labels an answer offers.
 *
 * @param result - The answer.
 * @returns Each option's label.
 */
function labels(result: CompletionResult | null): string[] {
  return result === null ? [] : result.options.map((option) => option.label);
}

describe("catalogCompletion", () => {
  it("offers namespaces after FROM", async () => {
    const result = await complete("select * from go|");

    expect(labels(result)).toEqual(["gold", "raw"]);
    expect(result?.from).toBe("select * from ".length);
    expect(result?.options[0]?.type).toBe("namespace");
    expect(labels(await complete("select * from t join |"))).toEqual(["gold", "raw"]);
    // Elsewhere a word is SQL's, and the language's keywords answer.
    expect(await complete("sel|")).toBeNull();
  });

  it("offers tables after a namespace and a dot", async () => {
    const result = await complete("select * from gold.st|");

    expect(labels(result)).toEqual(["sales", "daily_sales", "station_ranking"]);
    expect(result?.from).toBe("select * from gold.".length);
    expect(result?.options.map((option) => option.type)).toEqual(["namespace", "class", "class"]);
    expect(labels(await complete("select * from raw.|"))).toEqual(["weather"]);
    expect(labels(await complete("select * from raw.weather.|"))).toEqual([]);
    expect(await complete("select * from nowhere.|")).toBeNull();
  });

  it("reads no table's columns after a path that names a namespace", async () => {
    const catalog = aCatalog();
    const columns = vi.spyOn(catalog, "columns");

    expect(labels(await complete("select * from gold.sales.|", catalog))).toEqual(["fact_sales"]);
    expect(columns).not.toHaveBeenCalled();
  });

  it("offers a table's columns after its alias and a dot, with types", async () => {
    const catalog = aCatalog();
    const columns = vi.spyOn(catalog, "columns");
    const result = await complete("select r.| from gold.station_ranking as r", catalog);

    expect(labels(result)).toEqual(["station_id", "rank"]);
    expect(result?.options.map((option) => [option.type, option.detail])).toEqual([
      ["property", "long"],
      ["property", "int"],
    ]);
    expect(columns).toHaveBeenCalledWith("gold", "station_ranking");

    // Without AS, by the table's own name, and by its whole name.
    expect(labels(await complete("select s.ra| from gold.station_ranking s"))).toEqual([
      "station_id",
      "rank",
    ]);
    expect(
      labels(await complete("select f.| from gold.station_ranking s join gold.sales.fact_sales f")),
    ).toContain("sale_id");
    expect(labels(await complete("select station_ranking.| from gold.station_ranking"))).toEqual([
      "station_id",
      "rank",
    ]);
    expect(labels(await complete("select gold.sales.fact_sales.|"))).toContain("amount");
    // A keyword after the table is not its alias, and another statement's aliases do not count.
    expect(await complete("select where.| from gold.station_ranking where true")).toBeNull();
    expect(await complete("select * from gold.station_ranking s; select s.|")).toBeNull();
    expect(labels(await complete("select s.| from gold.station_ranking s; select 1"))).toEqual([
      "station_id",
      "rank",
    ]);
    // A table the person may not read has no columns to offer.
    expect(await complete("select w.| from raw.weather.daily w")).toBeNull();
  });

  it("offers the columns of the tables the statement reads for a word without its table", async () => {
    const catalog = aCatalog();
    const columns = vi.spyOn(catalog, "columns");
    const result = await complete("select * from gold.station_ranking where ra|", catalog);

    expect(labels(result)).toEqual(["station_id", "rank"]);
    expect(result?.from).toBe("select * from gold.station_ranking where ".length);
    expect(result?.options.map((option) => [option.type, option.detail, option.apply])).toEqual([
      ["property", "long", undefined],
      ["property", "int", undefined],
    ]);
    expect(columns).toHaveBeenCalledWith("gold", "station_ranking");
    // Every table of a join, the first without an alias too.
    expect(
      labels(
        await complete("select sa| from gold.station_ranking join gold.sales.fact_sales on true"),
      ),
    ).toEqual(["station_id", "rank", ...TABLE.schema.map((column) => column.name)]);
    expect(
      labels(await complete("select f.| from gold.station_ranking join gold.sales.fact_sales f")),
    ).toContain("sale_id");
    // Before FROM too, and through an alias: the whole statement counts.
    expect(labels(await complete("select ra| from gold.station_ranking r"))).toEqual([
      "station_id",
      "rank",
    ]);
    // Ctrl Space asks without a word; a space alone does not.
    expect(await complete("select * from gold.station_ranking where |")).toBeNull();
    expect(
      labels(await complete("select * from gold.station_ranking where |", aCatalog(), true)),
    ).toEqual(["station_id", "rank"]);
  });

  it("offers nothing of its own for a word without its table where no table is read", async () => {
    // The table's alias is being written, and SQL's keywords answer the rest.
    expect(await complete("select * from gold.station_ranking s|")).toBeNull();
    expect(await complete("select * from gold.station_ranking as s|")).toBeNull();
    expect(await complete("select ra|")).toBeNull();
    expect(await complete("select ra| from raw.weather.daily")).toBeNull();
    // Another statement's tables do not count.
    expect(await complete("select * from gold.station_ranking; select ra|")).toBeNull();
  });

  it("reads a table written twice by the same name once", async () => {
    const catalog = aCatalog();
    const columns = vi.spyOn(catalog, "columns");
    const result = await complete(
      "select ra| from gold.station_ranking join gold.station_ranking using (rank)",
      catalog,
    );

    expect(result?.options.map((option) => [option.label, option.apply])).toEqual([
      ["station_id", undefined],
      ["rank", undefined],
    ]);
    expect(columns).toHaveBeenCalledTimes(1);
  });

  it("offers a name that several tables have once per table, named by its table, and inserts it with the table", async () => {
    const catalog = aCatalog();
    catalog.columns = vi.fn((namespace: string, table: string) =>
      Promise.resolve(
        namespace === "gold" && table === "station_ranking"
          ? STATION_COLUMNS
          : namespace === "gold.sales"
            ? [{ ...(TABLE.schema[0] as ColumnOut), name: "station_id", type: "string" }]
            : null,
      ),
    );
    const result = await complete(
      "select st| from gold.station_ranking s join gold.sales.fact_sales",
      catalog,
    );

    expect(result?.options.map((option) => [option.label, option.detail, option.apply])).toEqual([
      ["station_id", "s · long", "s.station_id"],
      ["rank", "int", undefined],
      ["station_id", "gold.sales.fact_sales · string", "gold.sales.fact_sales.station_id"],
    ]);

    // The same table twice, by two aliases: once per alias.
    const self = await complete(
      "select ra| from gold.station_ranking a join gold.station_ranking b on a.rank = b.rank",
    );
    expect(self?.options.map((option) => [option.apply, option.detail])).toEqual([
      ["a.station_id", "a · long"],
      ["a.rank", "a · int"],
      ["b.station_id", "b · long"],
      ["b.rank", "b · int"],
    ]);
  });

  it("a description shows as text, never HTML", async () => {
    const result = await complete("select r.| from gold.station_ranking r");
    const [plain, described] = result?.options ?? [];

    expect(plain?.info).toBeUndefined();
    const info = (described?.info as (option: Completion) => Node)(described as Completion);
    expect(info).toBeInstanceOf(Text);
    expect(info.textContent).toBe('<img src=x onerror="alert(1)"> Rank by ridesU+202E');
  });

  it("shows a hidden character in a name as its mark and inserts the name itself", async () => {
    const catalog = aCatalog();
    catalog.tables = vi.fn(() => Promise.resolve(["sta\u200Btion"]));

    const result = await complete("select * from gold.|", catalog);
    const option = result?.options.find((each) => each.type === "class");

    expect(option?.label).toBe("sta\u200Btion");
    expect(option?.displayLabel).toBe("staU+200Btion");
  });
});
