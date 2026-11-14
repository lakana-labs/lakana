/**
 * The Overview's health block: the table's verdict in a word, and what is wrong in sentences.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { HealthSummaryOut, TableHealthOut } from "../../api";
import { useFocusAfter } from "../../shell/useFocusAfter";
import type { Resource } from "../../shell/useResource";
import { StatusGlyph } from "../../status/StatusGlyph";
import { ReadFailed } from "../cells";
import { HealthWord } from "../HealthWord";
import { firedLines, judged } from "./health";

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
 * sentence that says the table is healthy or has not been judged.
 *
 * @param props - The health.
 * @param props.health - The health the server answered.
 * @returns The lines or the sentence.
 */
function Lines({ health }: { readonly health: TableHealthOut }): JSX.Element {
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
          {t("table.healthBlock.more", { count: lines.length - MOST_LINES })}
        </p>
      )}
    </>
  );
}

/**
 * Renders the heading, the verdict's word from the table itself, and the lines from the health
 * read. A read that fails says why beside Try again and leaves the rest of the page as it is;
 * once it succeeds the focus goes to the heading, because the button is gone.
 *
 * @param props - The verdict and the read.
 * @param props.summary - The table's own verdict.
 * @param props.resource - What the health read answered.
 * @returns The block.
 */
export function HealthBlock({ summary, resource }: HealthBlockProps): JSX.Element {
  const { t } = useTranslation();
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(resource.data);
  return (
    <section className="health-block">
      <h2 className="block-heading" tabIndex={-1} ref={headingRef}>
        {t("table.healthBlock.heading")}
      </h2>
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
        resource.data !== undefined && <Lines health={resource.data} />
      )}
    </section>
  );
}
