import { describe, expect, it } from "vitest";

import { browserTargets } from "./targets";

describe("browserTargets", () => {
  it("maps browserslist entries to build targets", () => {
    expect(browserTargets(["chrome 141", "safari 18.5"])).toEqual(["chrome141", "safari18.5"]);
  });

  it("resolves queries such as Firefox ESR through browserslist", () => {
    const targets = browserTargets(["Firefox ESR"]);

    expect(targets.length).toBeGreaterThan(0);
    expect(targets.every((target) => /^firefox\d+(\.\d+)?$/.test(target))).toBe(true);
  });

  it("keeps only the oldest version of each browser, the minimum the build supports", () => {
    expect(browserTargets(["chrome 157", "chrome 156", "edge 156", "edge 155"])).toEqual([
      "chrome156",
      "edge155",
    ]);
  });

  it("takes the oldest version of a range, so the build supports all of it", () => {
    expect(browserTargets(["safari 15.2-15.3"])).toEqual(["safari15.2"]);
  });
});
