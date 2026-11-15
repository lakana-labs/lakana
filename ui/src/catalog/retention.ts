/**
 * The sentence that states a table's retention, shared by the Retention dialog and the rows of
 * the Expiring view.
 *
 * @packageDocumentation
 */
import i18next, { type TFunction } from "i18next";

import type { TableRetentionOut } from "../api";
import { markedText } from "../shell/VisibleText";
import "./views/viewStrings";

/**
 * Formats a day (`YYYY-MM-DD`) as the person's language writes it. The server reads the day as
 * UTC, so it is formatted in UTC: a local zone would show the day before.
 *
 * @param day - The day.
 * @returns The day in words.
 */
function dayWords(day: string): string {
  return new Intl.DateTimeFormat(i18next.language, {
    dateStyle: "long",
    timeZone: "UTC",
  }).format(new Date(`${day.slice(0, 10)}T00:00:00Z`));
}

/**
 * States the rule of a table and who set it, for the table or for its environment.
 *
 * @param t - The translation function.
 * @param r - The table's retention, with a source.
 * @returns The rule and who set it.
 */
function ruleWords(t: TFunction, r: TableRetentionOut): string {
  let rule = t("catalog.views.retention.never");
  if (r.days !== null) {
    rule = t("catalog.views.retention.days", { count: r.days });
  } else if (r.on !== null) {
    rule = t("catalog.views.retention.on", { date: dayWords(r.on) });
  }
  const by = r.set_by === null ? "" : "By";
  const place = r.source === "table" ? "setForTable" : "setForEnvironment";
  return t(`catalog.views.retention.${place}${by}`, { rule, who: markedText(r.set_by ?? "") });
}

/**
 * States the retention that applies to a table. A protected environment replaces the rule, which
 * is moot there; a deployed project keeps the table now, so its sentence says so and then states
 * the rule that applies once the project no longer builds it; with no rule at all the table
 * never expires; otherwise it is the rule and who set it.
 *
 * @param t - The translation function.
 * @param r - The table's retention, as the server reports it.
 * @returns The sentence.
 */
export function retentionWords(t: TFunction, r: TableRetentionOut): string {
  if (r.kept === "protected") {
    return t("catalog.views.retention.protected");
  }
  if (r.kept === "project_asset") {
    return r.source === null
      ? t("catalog.views.retention.projectAssetOnly")
      : t("catalog.views.retention.projectAsset", { rule: ruleWords(t, r) });
  }
  return r.source === null ? t("catalog.views.retention.none") : ruleWords(t, r);
}
