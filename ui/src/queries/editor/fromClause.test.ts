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
    ["at (time travel)", "select | from gold.a at (version => 1)"],
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

  it("reads past a parenthesis that is never closed, as one being typed", () => {
    expect(inScope("select sum(r.| from gold.station_ranking r")).toEqual([
      "r=gold.station_ranking",
    ]);
    expect(inScope("select round(r.rank, r.| from gold.a r")).toEqual(["r=gold.a"]);
    expect(inScope("select sum(| from gold.a r")).toEqual(["r=gold.a"]);
    // Closed and unclosed together: the closed ones are groups, the others are not.
    expect(
      inScope("select round(sum(r.rank), | from gold.a r join (select 1 from gold.b b) q"),
    ).toEqual(["r=gold.a"]);
    expect(inScope("select * from gold.a a where x in (select | from gold.b b")).toEqual([
      "a=gold.a",
      "b=gold.b",
    ]);
  });

  it.each([
    [
      "a WHERE subquery",
      "select * from gold.o o where id in (select max(a.| from gold.a a)",
      ["o=gold.o", "a=gold.a"],
    ],
    [
      "a WHERE subquery, bare",
      "select * from gold.o o where id in (select max(i| from gold.a a)",
      ["o=gold.o", "a=gold.a"],
    ],
    ["a CTE", "with x as (select max(a.| from gold.a a) select * from x", ["a=gold.a"]],
    ["a CTE, bare", "with x as (select sum(i| from gold.a a) select * from gold.b b", ["a=gold.a"]],
    ["a derived table", "select * from (select sum(a.| from gold.a a) q", ["a=gold.a"]],
    [
      "a derived table, bare",
      "select * from (select sum(i| from gold.a a) q join gold.b b on true",
      ["a=gold.a"],
    ],
    [
      "a scalar subquery",
      "select a.ax, (select max(b.| from gold.b b) from gold.a a",
      ["b=gold.b", "a=gold.a"],
    ],
    [
      "a scalar subquery, bare",
      "select a.ax, (select max(i| from gold.b b) from gold.a a",
      ["b=gold.b", "a=gold.a"],
    ],
    [
      "two calls in a subquery",
      "select * from gold.o o where id in (select coalesce(max(a.| from gold.a a)",
      ["o=gold.o", "a=gold.a"],
    ],
    [
      "a closed call after the cursor",
      "select * from gold.o o where id in (select max(a.| from gold.a a where f(a.id) > 0)",
      ["o=gold.o", "a=gold.a"],
    ],
  ])("reads past a call being typed inside %s", (_, sql, tables) => {
    expect(inScope(sql)).toEqual(tables);
  });

  it("takes any word after AS for the alias, a keyword too", () => {
    expect(inScope("select * from gold.a as at where at.|")).toEqual(["at=gold.a"]);
    expect(inScope("select * from gold.a as asof join gold.b b on true where |")).toEqual([
      "asof=gold.a",
      "b=gold.b",
    ]);
  });

  it("keeps a CTE's body and a derived table to themselves, and lets LATERAL see the tables to its left", () => {
    expect(inScope("with x as (select | from gold.t s) select * from gold.f f")).toEqual([
      "s=gold.t",
    ]);
    expect(inScope("select * from (select | from gold.t s) q join gold.f f on true")).toEqual([
      "s=gold.t",
    ]);
    expect(inScope("select * from gold.f f, lateral (select | from gold.t s)")).toEqual([
      "f=gold.f",
      "s=gold.t",
    ]);
    expect(
      inScope("select * from gold.f f join lateral (select | from gold.t s) q on true"),
    ).toEqual(["f=gold.f", "s=gold.t"]);
  });

  it("gives each branch of a UNION, INTERSECT or EXCEPT its own tables", () => {
    expect(inScope("select a.x from gold.a a union all select | from gold.b b")).toEqual([
      "b=gold.b",
    ]);
    expect(inScope("select | from gold.a a intersect select * from gold.b b")).toEqual([
      "a=gold.a",
    ]);
    expect(
      inScope("select * from gold.a a except select * from gold.b b except select | from gold.c c"),
    ).toEqual(["c=gold.c"]);
    // A star's EXCEPT is no set operator.
    expect(inScope("select * except (x), | from gold.a a")).toEqual(["a=gold.a"]);
  });

  it("reads the tables of UPDATE … FROM, DELETE … USING and a join in parentheses", () => {
    expect(inScope("update gold.a set x = b.y from gold.b b where |")).toEqual([
      "gold.a=gold.a",
      "b=gold.b",
    ]);
    expect(inScope("delete from gold.a using gold.b b where |")).toEqual([
      "gold.a=gold.a",
      "b=gold.b",
    ]);
    expect(inScope("select | from gold.a a join (gold.b b join gold.c c on true) on true")).toEqual(
      ["a=gold.a", "b=gold.b", "c=gold.c"],
    );
    // A join's USING names columns, not tables.
    expect(inScope("select | from gold.a a join gold.b b using (id)")).toEqual([
      "a=gold.a",
      "b=gold.b",
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
    ["update ", "table"],
    ["delete from gold.a using ", "table"],
    ["select * from gold.a where x in (select y from ", "table"],
    ["select sum(", "other"],
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
