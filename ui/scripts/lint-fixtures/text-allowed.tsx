/**
 * No violation, for `lint-ban.test.ts`: text from `t()`, glyphs, punctuation and the attributes no
 * one reads (class names, test IDs, keys, styles, ids, types, roles, links) stay allowed. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

/**
 * Renders a list of names with translated words around them.
 *
 * @param props - The names.
 * @param props.names - The names to list.
 * @param props.count - How many there are in all.
 * @returns The list.
 */
export function Names({
  names,
  count,
}: {
  readonly names: readonly string[];
  readonly count: number;
}): JSX.Element {
  const { t } = useTranslation();
  return (
    <section className="names stack" data-testid="names-list" aria-labelledby="names-heading">
      <h2 id="names-heading" title={t("names.title")}>
        {t("names.heading", { count })} · {count > 0 ? "—" : "…"}
      </h2>
      <input type="search" aria-label={t("names.search")} placeholder={t("names.example")} />
      <ul style={{ display: "grid" }}>
        {names.map((name) => (
          <li key={`name-${name}`} className={name === "" ? "empty" : "name"}>
            {name} ×
          </li>
        ))}
      </ul>
      <a href="/catalog" rel="noreferrer">
        {t("names.more")} →
      </a>
    </section>
  );
}
