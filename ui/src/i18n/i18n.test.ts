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
  expect(i18n.t("catalog.places.note.artifactDropped", { artifact: "ml.model" })).toBe(
    "Dropped artifact ml.model. Its files are removed from storage, and Lakana's periodic clean-up removes any it could not.",
  );
});

it("ships no string of an artifact's versions in the first load, and importing VersionsTab registers them", async () => {
  const i18n = initI18n();

  expect(i18n.exists("table.artifact.versions.heading")).toBe(false);

  await import("../catalog/artifact/VersionsTab");

  expect(i18n.t("table.artifact.versions.heading")).toBe("Versions");
  expect(i18n.t("table.artifact.files.mismatch", { path: "models/model.pkl" })).toBe(
    "models/model.pkl did not arrive whole, so nothing was saved. Download it again, or use the command above.",
  );
});

it("ships no string of the result grid in the first load, and importing ResultGrid registers them", async () => {
  const i18n = initI18n();

  expect(i18n.exists("grid.valueHint")).toBe(false);

  await import("../shell/ResultGrid");

  expect(i18n.t("grid.valueHint")).toBe(
    "Move to a cell with the arrow keys, or click one, to see its whole value here.",
  );
});

it("ships no string of the Queries screen in the first load, and importing it registers them", async () => {
  const i18n = initI18n();

  expect(i18n.exists("queries.run")).toBe(false);

  await import("../queries/QueriesScreen");

  expect(i18n.t("queries.run")).toBe("Run");
  expect(i18n.t("queries.result.label", { name: "Daily sales" })).toBe("Result of Daily sales");
  expect(i18n.t("grid.valueHint")).toBe(
    "Move to a cell with the arrow keys, or click one, to see its whole value here.",
  );
});

it("ships no string of the Jobs screen in the first load, and importing it registers them", async () => {
  const i18n = initI18n();

  expect(i18n.exists("jobs.runNow")).toBe(false);

  await import("../jobs/JobsScreen");

  expect(i18n.t("jobs.runNow")).toBe("Run now");
});

it("ships no string of the Connections screen in the first load, and importing it registers them", async () => {
  const i18n = initI18n();

  expect(i18n.exists("connections.list.add")).toBe(false);

  await import("../connections/ConnectionsScreen");

  expect(i18n.t("connections.list.add")).toBe("Add connection");
  expect(i18n.t("connections.status.ok", { when: "2 hours ago" })).toBe("OK · 2 hours ago");
});

it("marks a hidden character in every interpolated value, and leaves other values as they are", () => {
  const i18n = initI18n();
  i18n.addResource(
    "en",
    "translation",
    "probe.sentence",
    "{{name}} has {{count}} rows since {{date}}",
  );

  expect(
    i18n.t("probe.sentence", { name: "fact_\u202Eselas", count: 3, date: "Nov 8, 2026" }),
  ).toBe("fact_U+202Eselas has 3 rows since Nov 8, 2026");
  // Marking is idempotent: a value already marked at its call site is not marked twice.
  expect(i18n.t("probe.sentence", { name: "fact_U+202Eselas", count: 3, date: "x" })).toBe(
    "fact_U+202Eselas has 3 rows since x",
  );
  // Markup stays text: React escapes it where the sentence is shown.
  expect(i18n.t("probe.sentence", { name: "<b>x</b>", count: 1, date: "&" })).toBe(
    "<b>x</b> has 1 rows since &",
  );
});

it("keeps a command to copy exact: its values are not marked, CopyLine shows the marks", () => {
  const i18n = initI18n();

  expect(i18n.t("access.recovery.command", { name: "k\u202Eim" })).toBe(
    "lakana recovery create k\u202Eim",
  );
});

it("names Access as a place of Settings, not an area or a tour step, and ships the places' names with Settings", async () => {
  const i18n = initI18n();

  expect(i18n.exists("tour.steps.access")).toBe(false);
  expect(i18n.exists("areas.access.name")).toBe(false);
  expect(i18n.exists("settings.places.people")).toBe(false);

  await import("../settings/SettingsScreen");

  expect(i18n.t("settings.places.people")).toBe("People");
});

it("the environment picker registers its own strings", async () => {
  const i18n = initI18n();

  await import("../shell/EnvironmentPicker");

  expect(i18n.t("environment.protected")).toBe("protected");
  expect(i18n.t("environment.label")).toBe("Environment");
});
