import { Text } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { placeAfter, statementAt, tablesRead } from "./fromClause";

/**
 * What the tables in scope at the `|` are called, as `qualifier=namespace.table`.
 *
 * @param text - The SQL with a `|` where the cursor is.
 * @returns The tables.
 */
function inScope(text: string): string[] {
  const at = text.indexOf("|");
  return tablesRead({ text: text.replace("|", ""), at }).map(
    ({ qualifier, at: table }) => `${qualifier}=${table.namespace}.${table.table}`,
  );
}

describe("tablesRead", () => {
  it("reads every table of a comma list and of joins, with their aliases", () => {
    expect(inScope("select | from gold.a x, gold.b y, gold.c")).toEqual([
      "x=gold.a",
      "y=gold.b",
      "gold.c=gold.c",
    ]);
    expect(
      inScope("select | from gold.a join gold.b b on a.id = b.id left join gold.c as c"),
    ).toEqual(["gold.a=gold.a", "b=gold.b", "c=gold.c"]);
  });

  it.each([
    ["asof", "select | from gold.a asof join gold.b b on true"],
    ["positional", "select | from gold.a positional join gold.b b"],
    ["lateral", "select | from gold.a, lateral gold.b b"],
    ["tablesample", "select | from gold.a tablesample 10% join gold.b b on true"],
  ])("takes no %s for an alias", (_, sql) => {
    expect(inScope(sql)).toEqual(["gold.a=gold.a", "b=gold.b"]);
  });

  it.each([
    ["select (FROM first)", "from gold.a select |"],
    ["pivot", "select | from gold.a pivot (sum(x) for y in (1, 2))"],
    ["unpivot", "select | from gold.a unpivot (v for k in (x, y))"],
    ["offset", "select | from gold.a limit 1 offset 2"],
    ["returning", "delete from gold.a returning |"],
  ])("takes no %s for an alias", (_, sql) => {
    expect(inScope(sql)).toEqual(["gold.a=gold.a"]);
  });

  it("keeps a subquery's and a CTE's tables inside their parentheses", () => {
    expect(inScope("select | from (select * from gold.t s) q join gold.t t on true")).toEqual([
      "t=gold.t",
    ]);
    expect(inScope("with x as (select * from gold.t s) select | from x")).toEqual([]);
    // Inside, the tables around count too: a subquery may name them.
    expect(inScope("select * from gold.f f where exists (select | from gold.t s)")).toEqual([
      "f=gold.f",
      "s=gold.t",
    ]);
    expect(inScope("select * from gold.f f where x in (select y from gold.t s) and |")).toEqual([
      "f=gold.f",
    ]);
  });

  it("reads no table after a FROM that starts no clause", () => {
    expect(inScope("select extract(year from t.ts), | from gold.a a")).toEqual(["a=gold.a"]);
    expect(inScope("select | from gold.a a where a.x is distinct from b.y and true")).toEqual([
      "a=gold.a",
    ]);
    expect(inScope("select trim(both 'x' from t.name) |")).toEqual([]);
  });

  it("reads comments, strings and quoted names as blanks", () => {
    expect(inScope("select | -- from gold.b b\n from gold.a a /* join gold.c c */")).toEqual([
      "a=gold.a",
    ]);
    expect(inScope('select \'from gold.b\', | from "gold"."x" q, gold.a')).toEqual([
      "gold.a=gold.a",
    ]);
  });

  it("names a table read twice once, and a stray closing parenthesis changes nothing", () => {
    expect(inScope("select | from gold.a join gold.a using (id))")).toEqual(["gold.a=gold.a"]);
  });
});

describe("placeAfter", () => {
  it.each([
    ["select * from ", "table"],
    ["select * from gold.a, ", "table"],
    ["select * from gold.a join ", "table"],
    ["select * from gold.a join lateral ", "table"],
    ["select * from gold.a ", "alias"],
    ["select * from gold.a as ", "alias"],
    ["select * from (select 1) ", "alias"],
    ["select * from gold.a a ", "other"],
    ["select * from gold.a where ", "other"],
    ["select extract(year from ", "other"],
    ["select ", "other"],
  ])("after %j the cursor writes %s", (before, place) => {
    expect(placeAfter(before)).toBe(place);
  });
});

describe("statementAt", () => {
  it("is the text between the semicolons, with the position in it", () => {
    expect(statementAt(Text.of(["select 1; select 2; select 3"]), 13)).toEqual({
      text: " select 2",
      at: 4,
    });
  });
});
