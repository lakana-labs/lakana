import { describe, expect, it } from "vitest";

import { ALL_SHIPPED, AREAS, type AreaId, visibleAreas } from "./areas";
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
      "Access",
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
});
