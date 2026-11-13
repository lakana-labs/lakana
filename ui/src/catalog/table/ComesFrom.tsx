/**
 * The Overview's Comes from and goes to: the tables directly upstream and downstream, as links.
 *
 * @packageDocumentation
 */
import { type JSX, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { GraphOut } from "../../api";
import { useFocusAfter } from "../../shell/useFocusAfter";
import type { Resource } from "../../shell/useResource";
import { ReadFailed } from "../cells";
import { splitDotted, tablePath } from "../paths";
import { useTableData } from "./TableContext";
import { neighbourTables } from "./lineage";

/** The most tables shown each way before the rest are counted. */
const MOST_TABLES = 5;

/** Properties of {@link ComesFrom}. */
export interface ComesFromProps {
  /** What feeds the table. */
  readonly up: Resource<GraphOut>;
  /** What the table feeds. */
  readonly down: Resource<GraphOut>;
}

/** Properties of {@link Side}. */
interface SideProps {
  /** The side's name: Comes from or Goes to. */
  readonly label: string;
  /** What to say when there is nothing there; neutral, because a hidden table is simply absent. */
  readonly nothing: string;
  /** The answer for this side; undefined while it is read. */
  readonly graph: GraphOut | undefined;
}

/**
 * One side: the tables as links, at most five, then how many more, or the sentence for none.
 *
 * @param props - The name, the empty sentence and the answer.
 * @param props.label - The side's name.
 * @param props.nothing - What to say when nothing is there.
 * @param props.graph - The answer, once it arrived.
 * @returns The side.
 */
function Side({ label, nothing, graph }: SideProps): JSX.Element | null {
  const { t } = useTranslation();
  const { ref } = useTableData();
  if (graph === undefined) {
    return null;
  }
  const tables = neighbourTables(graph);
  return (
    <div className="comes-side">
      <h3 className="block-subheading">{label}</h3>
      {tables.length === 0 ? (
        <p>{nothing}</p>
      ) : (
        <ul className="plain-list">
          {tables.slice(0, MOST_TABLES).map((node) => {
            const place = splitDotted(node.name);
            return (
              <li key={node.id}>
                {place === null ? (
                  node.name
                ) : (
                  <Link to={tablePath(ref.environment, place.namespace, place.name)}>
                    {node.name}
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {tables.length > MOST_TABLES && (
        <p className="second">{t("table.comes.more", { count: tables.length - MOST_TABLES })}</p>
      )}
    </div>
  );
}

/**
 * Renders the two sides under one heading, with "Lineage ›" to the Lineage tab beside it. A
 * side that cannot be read says why beside Try again, which reads only what failed; the focus
 * then goes to the heading, because the button is gone.
 *
 * @param props - The two reads.
 * @param props.up - What feeds the table.
 * @param props.down - What the table feeds.
 * @returns The section.
 */
export function ComesFrom({ up, down }: ComesFromProps): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const answers = useMemo(() => ({ up: up.data, down: down.data }), [up.data, down.data]);
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(answers);
  const failed = up.error ?? down.error;
  return (
    <section className="comes-from">
      {/* Lineage › sits beside the heading, so Overview keeps fitting one screen. */}
      <div className="block-head">
        <h2 className="block-heading" tabIndex={-1} ref={headingRef}>
          {t("table.comes.heading")}
        </h2>
        <Link to={tablePath(ref.environment, ref.namespace, ref.name, "lineage")}>
          {t("table.comes.lineage")}
        </Link>
      </div>
      {failed === null ? (
        <>
          <Side
            label={t("table.comes.from")}
            nothing={t("table.comes.nothingUp")}
            graph={up.data}
          />
          <Side
            label={t("table.comes.to")}
            nothing={t("table.comes.nothingDown")}
            graph={down.data}
          />
        </>
      ) : (
        <ReadFailed
          message={failed}
          onRetry={() => {
            armFocus();
            for (const side of [up, down]) {
              if (side.error !== null) {
                side.reload();
              }
            }
          }}
        />
      )}
    </section>
  );
}
