import { describe, expect, it } from "vitest";

import { locationForFile, locationForFolder } from "./rawLocation";

describe("locationForFile", () => {
  it.each([
    ["raw/orders", "2026-11-01.csv", "raw/orders/*.csv"],
    ["raw/orders", "export.CSV", "raw/orders/*.CSV"],
    ["raw/orders", "archive.csv.gz", "raw/orders/*.gz"],
    ["raw/orders", "README", "raw/orders/README"],
    ["raw/orders", ".hidden", "raw/orders/.hidden"],
    ["raw/orders", "odd[1].c*v", "raw/orders/*.c[*]v"],
    ["raw/orders", "odd?", "raw/orders/odd[?]"],
  ])("in %s, %s gives %s", (folder, name, location) => {
    expect(locationForFile(folder, name)).toBe(location);
  });
});

describe("locationForFolder", () => {
  it("takes the most common ending of the folder's files", () => {
    expect(
      locationForFolder("raw/feeds", ["a.csv", "b.csv", "c.CSV", "readme.txt", "d.txt", "e.csv"]),
    ).toBe("raw/feeds/*.csv");
  });

  it("on a tie prefers an ending with a format, then the first in order", () => {
    expect(locationForFolder("raw/a", ["x.txt", "y.json"])).toBe("raw/a/*.json");
    expect(locationForFolder("raw/a", ["x.txt", "y.log"])).toBe("raw/a/*.log");
    expect(locationForFolder("raw/a", ["x.parquet", "y.csv"])).toBe("raw/a/*.csv");
    expect(locationForFolder("raw/a", ["y.log", "x.txt"])).toBe("raw/a/*.log");
    expect(locationForFolder("raw/a", ["y.csv", "x.parquet"])).toBe("raw/a/*.csv");
  });

  it("matches every file when the files have no ending, and nothing without files", () => {
    expect(locationForFolder("raw/a", ["README", "LICENSE"])).toBe("raw/a/*");
    expect(locationForFolder("raw/a", [])).toBeNull();
  });
});
