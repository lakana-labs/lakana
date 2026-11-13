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

it("ships no string of a table's page in the first load, and the page registers them itself", async () => {
  const i18n = initI18n();

  expect(Object.keys(en.table)).toEqual(["loading", "retry"]);
  expect(i18n.exists("table.tabs.overview")).toBe(false);

  await import("../catalog/table/TablePage");

  expect(i18n.t("table.tabs.overview")).toBe("Overview");
  expect(i18n.t("table.loading")).toBe("Loading the table…");
});
