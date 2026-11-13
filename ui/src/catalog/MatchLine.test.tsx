import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TableMatchOut } from "../api";
import { MatchLine, showsMatch } from "./MatchLine";

const COLUMN: TableMatchOut = {
  kind: "column",
  matched: "name",
  column: "store_id",
  column_type: "int",
  snippet: null,
  language: null,
  translated: false,
};

/**
 * Renders a line.
 *
 * @param match - The fields that differ from a column-name hit.
 * @param q - The search text.
 * @returns The container.
 */
function line(match: Partial<TableMatchOut>, q = "store_id"): HTMLElement {
  return render(<MatchLine match={{ ...COLUMN, ...match }} q={q} />).container;
}

describe("MatchLine", () => {
  it("names the column and its type with the match marked", () => {
    const container = line({});

    expect(container).toHaveTextContent("Column store_id · int");
    expect(container.querySelectorAll("mark.match")).toHaveLength(1);
    expect(container.querySelector("mark")).toHaveTextContent("store_id");
  });

  it("adds the text around a match in a column's label or description", () => {
    const container = line({ matched: "description", snippet: "joins dim_store" });

    expect(container).toHaveTextContent("Column store_id · int · joins dim_store");
  });

  it("marks the match in the snippet, whatever the case", () => {
    const container = line(
      {
        kind: "table",
        matched: "description",
        column: null,
        column_type: null,
        snippet: "One row per STORE visit",
      },
      "store",
    );

    expect(container).toHaveTextContent("Description · One row per STORE visit");
    expect(container.querySelector("mark")).toHaveTextContent("STORE");
    expect(container.querySelectorAll("mark")).toHaveLength(1);
  });

  it("names the table's own text", () => {
    for (const [matched, word] of [
      ["label", "Label"],
      ["description", "Description"],
      ["grain", "Grain"],
    ] as const) {
      const container = line({
        kind: "table",
        matched,
        column: null,
        column_type: null,
        snippet: "per store",
      });

      expect(container).toHaveTextContent(`${word} · per store`);
    }
  });

  it("says the language of a translated text", () => {
    const container = line({
      kind: "table",
      matched: "label",
      column: null,
      column_type: null,
      snippet: "Filialen",
      language: "de",
      translated: true,
    });

    expect(container).toHaveTextContent("Label · Filialen · de");
  });

  it("leaves the text alone when it holds no occurrence of the search", () => {
    const container = line({ matched: "measure", snippet: "revenue" }, "xyz");

    expect(container).toHaveTextContent("Column store_id · int · revenue");
    expect(container.querySelector("mark")).toBeNull();
  });

  it("falls back to the field's own name for a field it has no word for", () => {
    const container = line({ kind: "table", matched: "owner", column: null, snippet: "kim" });

    expect(container).toHaveTextContent("owner · kim");
  });

  it("shows a line for everything but a name hit on the table", () => {
    expect(showsMatch(COLUMN)).toBe(true);
    expect(showsMatch({ ...COLUMN, kind: "table", matched: "label" })).toBe(true);
    expect(showsMatch({ ...COLUMN, kind: "table", matched: "name", column: null })).toBe(false);
  });

  it("shows what it has when the server leaves a part out", () => {
    expect(line({ column: null, column_type: null })).toHaveTextContent(/^Column$/);
    expect(
      line({ kind: "table", matched: "grain", column: null, column_type: null, snippet: null }),
    ).toHaveTextContent(/^Grain ·$/);
  });

  it("marks nothing for an empty search", () => {
    expect(line({}, "").querySelector("mark")).toBeNull();
  });
});
