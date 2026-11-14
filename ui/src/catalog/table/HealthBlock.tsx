/**
 * The Overview's health block: the table's verdict in a word, and what is wrong in sentences.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { HealthSummaryOut, TableHealthOut } from "../../api";
import { useFocusAfter } from "../../shell/useFocusAfter";
import type { Resource } from "../../shell/useResource";
import { StatusGlyph } from "../../status/StatusGlyph";
import { ReadFailed } from "../cells";
import { HealthWord } from "../HealthWord";
import { tablePath } from "../paths";
import { firedLines, judged } from "./health";
import { useTableData } from "./TableContext";

/** The most lines the block shows before it counts the rest. */
const MOST_LINES = 5;

const NOT_JUDGED: HealthSummaryOut = {
  state: "not_judged",
  item: "not_judged",
  text: "Not judged",
  others: 0,
};

/** Properties of {@link HealthBlock}. */
export interface HealthBlockProps {
  /** The table's own verdict, the headline; null counts as not judged. */
  readonly summary: HealthSummaryOut | null;
  /** What the health read answered. */
  readonly resource: Resource<TableHealthOut>;
}

/**
 * The sentences under the headline: what is wrong, worst first and at most five, else one
 * sentence that says the table is healthy or has not been judged. The count of the rest links to
 * the Health tab.
 *
 * @param props - The health and the Health tab's address.
 * @param props.health - The health the server answered.
 * @param props.more - The Health tab's address, which "and 2 more" opens.
 * @returns The lines or the sentence.
 */
function Lines({
  health,
  more,
}: {
  readonly health: TableHealthOut;
  readonly more: string;
}): JSX.Element {
  const { t } = useTranslation();
  const lines = firedLines(health, t);
  if (lines.length === 0) {
    const sentence = !judged(health)
      ? "table.healthBlock.notJudged"
      : health.hidden_checks > 0
        ? "table.healthBlock.healthyVisible"
        : "table.healthBlock.healthy";
    return <p>{t(sentence)}</p>;
  }
  return (
    <>
      <ul className="health-lines">
        {lines.slice(0, MOST_LINES).map((line, index) => (
          <li key={`${line.kind}:${String(index)}`}>
            <StatusGlyph kind={line.severity === "failing" ? "failed" : "warning"} />
            <span>{line.sentence}</span>
          </li>
        ))}
      </ul>
      {lines.length > MOST_LINES && (
        <p className="second">
          <Link to={more}>{t("table.healthBlock.more", { count: lines.length - MOST_LINES })}</Link>
        </p>
      )}
    </>
  );
}

/**
 * Renders the heading with Health details › beside it, the verdict's word from the table itself,
 * and the lines from the health read. A read that fails says why beside Try again and leaves the
 * rest of the page as it is; once it succeeds the focus goes to the heading, because the button
 * is gone.
 *
 * @param props - The verdict and the read.
 * @param props.summary - The table's own verdict.
 * @param props.resource - What the health read answered.
 * @returns The block.
 */
export function HealthBlock({ summary, resource }: HealthBlockProps): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(resource.data);
  const details = tablePath(ref.environment, ref.namespace, ref.name, "health");
  return (
    <section className="health-block">
      {/* Health details › sits beside the heading, so Overview keeps fitting one screen. */}
      <div className="block-head">
        <h2 className="block-heading" tabIndex={-1} ref={headingRef}>
          {t("table.healthBlock.heading")}
        </h2>
        <Link to={details}>{t("table.healthBlock.details")}</Link>
      </div>
      <p className="health-headline">
        <HealthWord summary={summary ?? NOT_JUDGED} />
      </p>
      {resource.error !== null ? (
        <ReadFailed
          message={resource.error}
          onRetry={() => {
            armFocus();
            resource.reload();
          }}
        />
      ) : (
        resource.data !== undefined && <Lines health={resource.data} more={details} />
      )}
    </section>
  );
}
