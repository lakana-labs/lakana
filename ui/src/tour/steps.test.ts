import { describe, expect, it } from "vitest";

import { areasFor, visibleAreas } from "../shell/areas";
import { tourSteps, tourTarget } from "./steps";

describe("tourSteps", () => {
  it("follows the sidebar, then Search and Help, for an administrator", () => {
    expect(tourSteps(visibleAreas(areasFor(true)))).toEqual([
      "home",
      "catalog",
      "queries",
      "jobs",
      "connections",
      "access",
      "settings",
      "search",
      "help",
    ]);
  });

  it("leaves out the areas a person cannot use", () => {
    expect(tourSteps(visibleAreas(areasFor(false)))).toEqual([
      "home",
      "catalog",
      "queries",
      "jobs",
      "connections",
      "search",
      "help",
    ]);
  });

  it("finds each step's place by its data-tour attribute", () => {
    expect(tourTarget("catalog")).toBe('[data-tour="catalog"]');
  });
});
