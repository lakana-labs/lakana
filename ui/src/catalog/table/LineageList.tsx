/**
 * The Lineage tab's text alternative: the same nodes and edges as the drawing, as a list.
 *
 * @packageDocumentation
 */
import { Fragment, type JSX, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { GraphNodeOut, GraphOut } from "../../api";
import { VisibleText } from "../../shell/VisibleText";
import { splitDotted, tablePath } from "../paths";
import { jobLabel, layout, type LineageBox } from "./layout";
import { mergeGraphs } from "./lineage";
import { kindName } from "./lineageStrings";

/** Properties of {@link LineageList}. */
export interface LineageListProps {
  /** What feeds the table. */
  readonly up: GraphOut;
  /** What the table feeds. */
  readonly down: GraphOut;
  /** The environment, for the links of the tables. */
  readonly environment: string;
}

/** Properties of {@link TableName}. */
interface TableNameProps {
  /** The table's node. */
  readonly node: GraphNodeOut;
  /** The environment. */
  readonly environment: string;
}

/**
 * A table's dotted name: a link to its page, or text for the table itself and for a name that
 * cannot be split into namespace and name.
 *
 * @param props - The table and the environment.
 * @param props.node - The table's node.
 * @param props.environment - The environment.
 * @returns The name.
 */
function TableName({ node, environment }: TableNameProps): JSX.Element {
  const place = node.distance > 0 ? splitDotted(node.name) : null;
  return place === null ? (
    <VisibleText text={node.name} />
  ) : (
    <Link to={tablePath(environment, place.namespace, place.name)}>
      <VisibleText text={node.name} />
    </Link>
  );
}

/**
 * Names joined with commas.
 *
 * @param names - Each name, already rendered, with its key.
 * @returns The names.
 */
function joined(names: readonly { key: string; shown: ReactNode }[]): JSX.Element {
  return (
    <>
      {names.map(({ key, shown }, index) => (
        <Fragment key={key}>
          {index > 0 && ", "}
          {shown}
        </Fragment>
      ))}
    </>
  );
}

/** Properties of {@link Builder}. */
interface BuilderProps {
  /** The asset's box. */
  readonly box: LineageBox;
  /** The tables it reads, in the drawing's order. */
  readonly reads: readonly GraphNodeOut[];
  /** The connections it uses, in the drawing's order. */
  readonly uses: readonly GraphNodeOut[];
  /** The environment. */
  readonly environment: string;
}

/**
 * One asset: "built by job daily_sales (asset gold.sales.fact_sales), which reads
 * staging.stg_sales", with the connections it uses under it.
 *
 * @param props - The asset, what it reads and uses, and the environment.
 * @param props.box - The asset's box.
 * @param props.reads - The tables it reads.
 * @param props.uses - The connections it uses.
 * @param props.environment - The environment.
 * @returns The item.
 */
function Builder({ box, reads, uses, environment }: BuilderProps): JSX.Element {
  const { t } = useTranslation();
  const asset = <VisibleText text={box.node.name} />;
  const jobs = joined(
    box.jobs.map((job) => ({ key: job.id, shown: <VisibleText text={jobLabel(job).job} /> })),
  );
  return (
    <li>
      {box.jobs.length === 0 ? (
        <Trans t={t} i18nKey="table.lineage.builtByNoJob" components={{ asset }} />
      ) : (
        <Trans
          t={t}
          i18nKey="table.lineage.builtBy"
          count={box.jobs.length}
          components={{ jobs, asset }}
        />
      )}
      {reads.length > 0 && (
        <Trans
          t={t}
          i18nKey="table.lineage.reads"
          components={{
            tables: joined(
              reads.map((node) => ({
                key: node.id,
                shown: <TableName node={node} environment={environment} />,
              })),
            ),
          }}
        />
      )}
      {uses.length > 0 && (
        <ul>
          {uses.map((connection) => {
            const kind = kindName(t, connection.connection_kind);
            return (
              <li key={connection.id}>
                <Trans
                  t={t}
                  i18nKey="table.lineage.uses"
                  components={{
                    name: <VisibleText text={connection.name} />,
                    kind: "word" in kind ? <>{kind.word}</> : <VisibleText text={kind.raw} />,
                  }}
                />
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

/**
 * Lists every table of the drawing, left to right, each with the asset that builds it, the job
 * that runs that asset, the tables it reads and the connections it uses. An asset that builds no
 * table of the drawing is an item of its own. Tables link to their page; jobs and connections are
 * text. Every name shows with its invisible characters marked.
 *
 * @param props - The two answers and the environment.
 * @param props.up - What feeds the table.
 * @param props.down - What the table feeds.
 * @param props.environment - The environment.
 * @returns The list.
 */
export function LineageList({ up, down, environment }: LineageListProps): JSX.Element {
  const { t } = useTranslation();
  const drawn = layout(up, down);
  const boxes = new Map(drawn.nodes.map((box) => [box.node.id, box]));
  const edges = mergeGraphs(up, down).edges;
  /**
   * The drawn nodes an edge of a kind leads to from a node, in the drawing's order.
   *
   * @param kind - The edge's kind.
   * @param source - The node it leaves.
   * @returns The nodes it reaches.
   */
  const from = (kind: string, source: string): GraphNodeOut[] =>
    drawn.nodes
      .filter((box) =>
        edges.some(
          (edge) => edge.kind === kind && edge.source === source && edge.target === box.node.id,
        ),
      )
      .map((box) => box.node);
  const builtBy = (table: string): LineageBox[] =>
    drawn.nodes.filter(
      (box) =>
        box.node.kind === "asset" &&
        edges.some(
          (edge) => edge.kind === "builds" && edge.source === box.node.id && edge.target === table,
        ),
    );
  const builder = (box: LineageBox): JSX.Element => (
    <Builder
      key={box.node.id}
      box={box}
      reads={from("reads", box.node.id)}
      uses={from("uses", box.node.id)}
      environment={environment}
    />
  );
  const builds = (box: LineageBox): boolean =>
    edges.some(
      (edge) => edge.kind === "builds" && edge.source === box.node.id && boxes.has(edge.target),
    );
  return (
    <ul className="plain-list lineage-list" aria-label={t("table.lineage.listLabel")}>
      {drawn.nodes.map((box) => {
        if (box.node.kind === "table") {
          const builders = builtBy(box.node.id);
          return (
            <li key={box.node.id}>
              <TableName node={box.node} environment={environment} />
              {box.node.distance === 0 && <> {t("table.lineage.thisTable")}</>}
              {builders.length > 0 && <ul>{builders.map(builder)}</ul>}
            </li>
          );
        }
        return box.node.kind === "asset" && !builds(box) ? builder(box) : null;
      })}
    </ul>
  );
}
