import { describe, expect, it } from "vitest";

import { runPath, savedPath } from "./paths";

describe("the Queries screen's addresses", () => {
  it("names a run by its environment and id, each part encoded", () => {
    expect(runPath("prod", "0b1c2d3e")).toBe("/queries/prod/runs/0b1c2d3e");
    expect(runPath("my env", "a/b")).toBe("/queries/my%20env/runs/a%2Fb");
  });

  it("names a saved query by its environment and id, each part encoded", () => {
    expect(savedPath("prod", "6a7b8c9d")).toBe("/queries/prod/saved/6a7b8c9d");
    expect(savedPath("dev?", "x#y")).toBe("/queries/dev%3F/saved/x%23y");
  });
});
