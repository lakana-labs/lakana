import { EditorState } from "@codemirror/state";
import { EditorView, type Tooltip } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ColumnOut } from "../../api/table";
import { TABLE } from "../../test/table";
import type { CompletionCatalog } from "./catalog";
import { columnHover, columnTooltip } from "./hover";

const COLUMNS: readonly ColumnOut[] = [
  {
    ...(TABLE.schema[0] as ColumnOut),
    name: "station_id",
    type: "long",
    description: "The station's id.<b>bold</b>",
  },
  {
    ...(TABLE.schema[1] as ColumnOut),
    name: "rank",
    type: "int",
    description: "Rank by rides\u202E",
  },
  { ...(TABLE.schema[2] as ColumnOut), name: "rides", type: "long", description: null },
];

const catalog: CompletionCatalog = {
  namespaces: () => Promise.resolve(["gold"]),
  tables: () => Promise.resolve([]),
  columns: vi.fn((namespace: string, table: string) =>
    Promise.resolve(namespace === "gold" && table === "station_ranking" ? COLUMNS : null),
  ),
};

let view: EditorView | null = null;

afterEach(() => {
  view?.destroy();
  view = null;
});

/**
 * Asks the hover at the `|` in a text.
 *
 * @param text - The SQL with a `|` where the pointer is.
 * @returns The tooltip, or null.
 */
async function hoverAt(text: string): Promise<Tooltip | null> {
  const pos = text.indexOf("|");
  view = new EditorView({
    state: EditorState.create({ doc: text.replace("|", "") }),
    parent: document.body,
  });
  return columnTooltip(catalog)(view, pos, 1) as Promise<Tooltip | null>;
}

/**
 * The lines a tooltip shows.
 *
 * @param tooltip - The tooltip.
 * @returns The text of each of its children.
 */
function lines(tooltip: Tooltip | null): (string | null)[] {
  if (tooltip === null || view === null) {
    throw new Error("no tooltip");
  }
  return Array.from(tooltip.create(view).dom.childNodes, (node) => node.textContent);
}

describe("columnHover", () => {
  it("hover over alias.column shows column, type and description", async () => {
    const sql = "select r.stat|ion_id from gold.station_ranking r";
    const tooltip = await hoverAt(sql);

    expect(tooltip?.pos).toBe("select r.".length);
    expect(tooltip?.end).toBe("select r.station_id".length);
    expect(lines(tooltip)).toEqual(["station_id · long", "The station's id.<b>bold</b>"]);
    const dom = tooltip?.create(view as EditorView).dom;
    expect(dom?.querySelector("b")).toBeNull();
  });

  it("shows a column by its table's whole name, and a hidden character as its mark", async () => {
    expect(lines(await hoverAt("select gold.station_ranking.ra|nk"))).toEqual([
      "rank · int",
      "Rank by ridesU+202E",
    ]);
    expect(lines(await hoverAt("select gold.station_ranking.rides|"))).toEqual(["rides · long"]);
  });

  it("explains a column written without its table when one table the statement reads has it", async () => {
    const sql = "select stat|ion_id from gold.station_ranking where rank < 10";
    const tooltip = await hoverAt(sql);

    expect(tooltip?.pos).toBe("select ".length);
    expect(tooltip?.end).toBe("select station_id".length);
    expect(lines(tooltip)).toEqual(["station_id · long", "The station's id.<b>bold</b>"]);
    expect(lines(await hoverAt("select * from gold.station_ranking r where ra|nk < 10"))).toEqual([
      "rank · int",
      "Rank by ridesU+202E",
    ]);
  });

  it("explains no column written without its table that no table or several tables have", async () => {
    expect(await hoverAt("select stat|ion_id from gold.weather")).toBeNull();
    expect(await hoverAt("select stat|ion_id")).toBeNull();
    expect(await hoverAt("select no|pe from gold.station_ranking")).toBeNull();
    // Both tables have it: which one is meant is not said.
    expect(
      await hoverAt(
        "select ra|nk from gold.station_ranking a join gold.station_ranking b on a.rank = b.rank",
      ),
    ).toBeNull();
    // The tables of a comma list count too.
    expect(
      await hoverAt("select ra|nk from gold.station_ranking a, gold.station_ranking b"),
    ).toBeNull();
    // A table out of scope does not count.
    expect(await hoverAt("select ra|nk from (select * from gold.station_ranking s) q")).toBeNull();
    // A word before a dot names a table, not a column.
    expect(await hoverAt("select ra|nk.x from gold.station_ranking")).toBeNull();
  });

  it("shows nothing over a name that is not a known column", async () => {
    expect(await hoverAt("sel|ect 1")).toBeNull();
    expect(await hoverAt("select |r.station_id from gold.station_ranking r")).toBeNull();
    expect(await hoverAt("select r.nope| from gold.station_ranking r")).toBeNull();
    expect(await hoverAt("select x.station_id| from gold.station_ranking r")).toBeNull();
    expect(await hoverAt("select w.station_id| from gold.weather w")).toBeNull();
  });

  it("reads no table for a path whose start names a namespace: its last part is a table", async () => {
    const nested: CompletionCatalog = {
      namespaces: () => Promise.resolve(["gold", "gold.sales"]),
      tables: () => Promise.resolve([]),
      columns: () => Promise.resolve(null),
    };
    const columns = vi.spyOn(nested, "columns");
    const pos = "select * from gold.sales.fact_sa".length;
    view = new EditorView({
      state: EditorState.create({ doc: "select * from gold.sales.fact_sales" }),
      parent: document.body,
    });

    expect(await columnTooltip(nested)(view, pos, 1)).toBeNull();
    expect(columns).not.toHaveBeenCalled();
  });

  it("is an extension the editor takes", () => {
    view = new EditorView({
      state: EditorState.create({ doc: "select 1", extensions: columnHover(catalog) }),
      parent: document.body,
    });
    expect(view.state.doc.toString()).toBe("select 1");
  });
});
