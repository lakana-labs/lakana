import { expect, it } from "vitest";

import { STATUS_KINDS } from "../status/status";
import { initI18n } from "./i18n";
import en from "./en.json";

it("every status kind has an English word", () => {
  const i18n = initI18n();

  for (const kind of STATUS_KINDS) {
    const word = i18n.t(`status.${kind}`);
    expect(word).not.toBe("");
    expect(word).not.toBe(`status.${kind}`);
  }
});

it("ships no string of a table's page in the first load, Health and Access included, and the page registers them itself", async () => {
  const i18n = initI18n();

  expect(Object.keys(en.table)).toEqual(["loading", "retry"]);
  expect(i18n.exists("table.tabs.overview")).toBe(false);
  expect(i18n.exists("table.healthTab.checks.heading")).toBe(false);
  expect(i18n.exists("table.access.mine.heading")).toBe(false);

  await import("../catalog/table/TablePage");

  expect(i18n.t("table.tabs.overview")).toBe("Overview");
  expect(i18n.t("table.loading")).toBe("Loading the table…");
  expect(i18n.t("table.healthTab.checks.heading")).toBe("Checks");
  expect(i18n.t("table.access.mine.heading")).toBe("What you may do");
});

it("ships no lineage string in the first load, and the Lineage tab registers its own", async () => {
  const i18n = initI18n();

  expect(i18n.exists("table.lineage.stepUp")).toBe(false);
  await import("../catalog/table/TablePage");
  expect(i18n.exists("table.lineage.stepUp")).toBe(false);

  await import("../catalog/table/LineageTab");

  expect(i18n.t("table.lineage.stepUp")).toBe("One step up");
});

it("ships no string of the views or the actions in the first load, and importing ViewBody registers them", async () => {
  const i18n = initI18n();

  expect(Object.keys(en.catalog)).not.toContain("views");
  expect(i18n.exists("catalog.views.dropped.empty")).toBe(false);
  expect(i18n.exists("table.actions.undrop.title")).toBe(false);
  expect(i18n.exists("table.actions.drop.title")).toBe(false);
  expect(i18n.exists("table.actions.maintain.title")).toBe(false);
  expect(i18n.exists("table.actions.retention.title")).toBe(false);
  expect(i18n.exists("catalog.views.retention.never")).toBe(false);
  expect(i18n.t("catalog.view.dropped")).toBe("Dropped");

  await import("../catalog/views/ViewBody");

  expect(i18n.t("catalog.views.dropped.empty", { place: "prod" })).toBe(
    "Nothing was dropped in prod that can still be restored.",
  );
  expect(i18n.t("table.actions.undrop.title", { table: "gold.sales.fact" })).toBe(
    "Restore gold.sales.fact",
  );
  expect(i18n.t("table.actions.drop.title", { table: "gold.sales.fact" })).toBe(
    "Drop gold.sales.fact?",
  );
  expect(i18n.t("table.actions.maintain.title", { table: "gold.sales.fact" })).toBe(
    "Maintain gold.sales.fact now",
  );
  expect(i18n.t("catalog.views.retention.never")).toBe("never");
});

it("ships no string of the place menu in the first load, and importing PlaceActions registers them", async () => {
  const i18n = initI18n();

  expect(Object.keys(en.catalog)).not.toContain("places");
  expect(i18n.exists("catalog.places.delete.title")).toBe(false);

  await import("../catalog/places/PlaceActions");

  expect(i18n.t("catalog.places.delete.title", { namespace: "silver" })).toBe(
    "Delete namespace silver?",
  );
  expect(i18n.t("catalog.places.note.created", { namespace: "gold.finance" })).toBe(
    "Created namespace gold.finance.",
  );
});
