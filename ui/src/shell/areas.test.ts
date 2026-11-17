import { describe, expect, it } from "vitest";

import { ALL_SHIPPED, AREAS, type AreaId, areasFor, visibleAreas } from "./areas";
import { initI18n } from "../i18n/i18n";

const i18n = initI18n();

describe("areas", () => {
  it("lists the sidebar in the decided order", () => {
    expect(visibleAreas(ALL_SHIPPED).map((area) => i18n.t(`areas.${area.id}.name`))).toEqual([
      "Home",
      "Catalog",
      "Queries",
      "Jobs",
      "Connections",
      "Settings",
    ]);
  });

  it("never shows Experiments before M8", () => {
    expect(visibleAreas(ALL_SHIPPED).map((area) => area.id)).not.toContain("experiments");
    expect(
      visibleAreas(new Set(AREAS.map((area) => area.id))).map((area) => area.id),
    ).not.toContain("experiments");
  });

  it("hides an area the user cannot use", () => {
    expect(visibleAreas(new Set<AreaId>(["home", "catalog"])).map((area) => area.id)).toEqual([
      "home",
      "catalog",
    ]);
  });

  it("gives every shipped area a purpose and a command line", () => {
    for (const area of visibleAreas(ALL_SHIPPED)) {
      expect(i18n.t(`areas.${area.id}.purpose`)).not.toBe(`areas.${area.id}.purpose`);
      expect(area.cli).toMatch(/^lakana /);
    }
  });

  it("gives Settings to everyone signed in", () => {
    expect([...areasFor(false)]).toEqual([...ALL_SHIPPED]);
    expect([...areasFor(false)]).toEqual([
      "home",
      "catalog",
      "queries",
      "jobs",
      "connections",
      "settings",
    ]);
  });

  it("hides the Catalog from a person with no access yet", () => {
    expect([...areasFor(true)]).toEqual(["home", "queries", "jobs", "connections", "settings"]);
    expect(areasFor(true).has("catalog")).toBe(false);
  });
});
