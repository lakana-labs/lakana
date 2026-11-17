import { describe, expect, it } from "vitest";

import { formatSql } from "./formatSql";

describe("formatSql", () => {
  it("formats DuckDB SQL with upper-case keywords", async () => {
    const sql =
      "select station_id, count(*) as rides from gold.station_ranking where rank <= 10 group by all";

    expect(await formatSql(sql)).toBe(
      [
        "SELECT",
        "  station_id,",
        "  count(*) AS rides",
        "FROM",
        "  gold.station_ranking",
        "WHERE",
        "  rank <= 10",
        "GROUP BY ALL",
      ].join("\n"),
    );
  });

  it("returns null for text it cannot parse and changes nothing", async () => {
    const sql = "select 'an unclosed string from gold.station_ranking";

    expect(await formatSql(sql)).toBeNull();
  });
});
