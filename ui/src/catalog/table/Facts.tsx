/**
 * The Overview's facts: who owns the table, how big it is, when it was written and read.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { useMe } from "../../shell/me";
import { OwnerName, RowCount, Unknown, When } from "../cells";
import { formatBytes } from "../format";
import { tablePath } from "../paths";
import { DefinedIn } from "./DefinedIn";
import { useTableData } from "./TableContext";

/** Properties of {@link Fact}. */
interface FactProps {
  /** The fact's name. */
  readonly term: string;
  /** What it says. */
  readonly children: ReactNode;
}

/**
 * One fact: its name and what it says.
 *
 * @param props - The name and the answer.
 * @param props.term - The fact's name.
 * @param props.children - What it says.
 * @returns The fact.
 */
function Fact({ term, children }: FactProps): JSX.Element {
  return (
    <div className="fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * Renders the facts as a description list. Where something is not known the fact says so in
 * words. Last read shows when and how many people read the table in the last 7 days, never who:
 * the names belong to the Health tab, which an administrator opens with Readers ›. Defined in
 * comes last, once its own read arrives.
 *
 * @returns The facts.
 */
export function Facts(): JSX.Element {
  const { t, i18n } = useTranslation();
  const { ref, table } = useTableData();
  const me = useMe();
  const size = table.size_bytes;
  const bytes = size === null ? null : formatBytes(size, i18n.language);
  const grain = table.model?.grain ?? null;
  return (
    <section className="facts">
      <h2 className="block-heading">{t("table.facts.heading")}</h2>
      <dl>
        <Fact term={t("table.facts.owner")}>
          <OwnerName owner={table.owner} />
        </Fact>
        <Fact term={t("table.facts.rows")}>
          <RowCount value={table.row_count} />
        </Fact>
        <Fact term={t("table.facts.size")}>
          {bytes === null || size === null ? (
            <Unknown words={t("catalog.notKnown")} />
          ) : (
            <span
              title={t("catalog.exactBytes", {
                bytes: new Intl.NumberFormat(i18n.language).format(size),
              })}
            >
              {table.file_count === null
                ? bytes
                : t("table.facts.sizeInFiles", { size: bytes, count: table.file_count })}
            </span>
          )}
        </Fact>
        <Fact term={t("table.facts.lastWrite")}>
          <When iso={table.last_write_at} never={t("catalog.neverWritten")} />
        </Fact>
        <Fact term={t("table.facts.lastRead")}>
          <When iso={table.last_read_at} never={t("catalog.neverRead")} />
          {table.last_read_at !== null && (
            <>
              {" "}
              <span aria-hidden="true">·</span>{" "}
              <span>{t("table.facts.readers", { count: table.readers_7d })}</span>
              {me.admin && (
                <>
                  {" "}
                  <Link
                    to={`${tablePath(ref.environment, ref.namespace, ref.name, "health")}#readers`}
                  >
                    {t("table.facts.readersLink")}
                  </Link>
                </>
              )}
            </>
          )}
        </Fact>
        <Fact term={t("table.facts.created")}>
          <When iso={table.created_at} never={t("catalog.notKnown")} />
        </Fact>
        {grain !== null && <Fact term={t("table.facts.grain")}>{grain}</Fact>}
        <DefinedIn place={ref} />
      </dl>
    </section>
  );
}
