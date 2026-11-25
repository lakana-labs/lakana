import { describe, expect, it } from "vitest";

import { ALL_SHIPPED, areasFor, visibleAreas } from "../shell/areas";
import { TOUR_GUIDES, tourSteps, tourTarget } from "./steps";

describe("tourSteps", () => {
  it("follows the sidebar, then Search and Help, with no step of its own for Access", () => {
    const steps = tourSteps(visibleAreas(ALL_SHIPPED));
    expect(steps).not.toContain("access");
    expect(steps).toEqual([
      "home",
      "catalog",
      "queries",
      "jobs",
      "connections",
      "settings",
      "search",
      "help",
    ]);
  });

  it("leaves out the areas a person cannot use", () => {
    expect(tourSteps(visibleAreas(areasFor(true)))).toEqual([
      "home",
      "queries",
      "jobs",
      "connections",
      "settings",
      "search",
      "help",
    ]);
  });

  it("finds each step's place by its data-tour attribute", () => {
    expect(tourTarget("catalog")).toBe('[data-tour="catalog"]');
  });
});

describe("TOUR_GUIDES", () => {
  // scripts/tests/test_user_text_urls.py checks that each page and heading exists.
  it("gives every step but Help a guide; Help keeps the further reading", () => {
    const steps = tourSteps(visibleAreas(ALL_SHIPPED));
    expect(Object.keys(TOUR_GUIDES).sort()).toEqual(steps.filter((step) => step !== "help").sort());
  });
});
