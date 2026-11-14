/**
 * The Lineage tab's text alternative: the same nodes and edges as the drawing, as a list.
 *
 * @packageDocumentation
 */
import { Fragment, type JSX, type ReactNode, useMemo } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { GraphNodeOut, GraphOut } from "../../api";
import { VisibleText } from "../../shell/VisibleText";
import { splitDotted, tablePath } from "../paths";
import { jobLabel, type LineageBox, type LineageLayout } from "./layout";
import { kindName } from "./lineageStrings";

/** An edge of a lineage answer. */
type Edge = GraphOut["edges"][number];

/** Properties of {@link LineageList}. */
export interface LineageListProps {
  /** The drawing's layout: the list names its boxes, in its order. */
  readonly layout: LineageLayout;
  /** The edges of both answers, merged (`mergeGraphs`). */
  readonly edges: readonly Edge[];
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
 * text. Every name shows with its invisible characters marked. The edges are indexed by their
 * source once, so a list of 500 nodes costs no more than reading its edges.
 *
 * @param props - The layout, the edges and the environment.
 * @param props.layout - The drawing's layout, whose boxes the list follows.
 * @param props.edges - The edges of both answers, merged.
 * @param props.environment - The environment.
 * @returns The list.
 */
export function LineageList({ layout: drawn, edges, environment }: LineageListProps): JSX.Element {
  const { t } = useTranslation();
  const { order, bySource } = useMemo(() => {
    const index = new Map(drawn.nodes.map((box, at) => [box.node.id, at]));
    // The edges leaving each drawn box; an edge from a node not drawn (a job) is not needed.
    const leaving = new Map(drawn.nodes.map((box) => [box.node.id, [] as Edge[]]));
    for (const edge of edges) {
      leaving.get(edge.source)?.push(edge);
    }
    return { order: index, bySource: leaving };
  }, [drawn, edges]);
  /**
   * The drawn boxes the edges of a kind lead to from a node, in the drawing's order.
   *
   * @param kind - The edge's kind.
   * @param source - The node it leaves.
   * @returns The boxes it reaches.
   */
  const from = (kind: string, source: string): LineageBox[] =>
    (bySource.get(source) as Edge[])
      .filter((edge) => edge.kind === kind)
      .map((edge) => order.get(edge.target))
      .filter((at) => at !== undefined)
      .sort((a, b) => a - b)
      .map((at) => drawn.nodes[at] as LineageBox);
  // Each drawn asset under the drawn table it builds, in the drawing's order.
  const builders = new Map<string, LineageBox[]>();
  const loose: LineageBox[] = [];
  for (const box of drawn.nodes) {
    if (box.node.kind === "asset") {
      const built = from("builds", box.node.id);
      if (built.length === 0) {
        loose.push(box);
      }
      for (const table of built) {
        builders.set(table.node.id, [...(builders.get(table.node.id) ?? []), box]);
      }
    }
  }
  const builder = (box: LineageBox): JSX.Element => (
    <Builder
      key={box.node.id}
      box={box}
      reads={from("reads", box.node.id).map((one) => one.node)}
      uses={from("uses", box.node.id).map((one) => one.node)}
      environment={environment}
    />
  );
  return (
    <ul className="plain-list lineage-list" aria-label={t("table.lineage.listLabel")}>
      {drawn.nodes.map((box) => {
        if (box.node.kind === "table") {
          const built = builders.get(box.node.id) ?? [];
          return (
            <li key={box.node.id}>
              <TableName node={box.node} environment={environment} />
              {box.node.distance === 0 && <> {t("table.lineage.thisTable")}</>}
              {built.length > 0 && <ul>{built.map(builder)}</ul>}
            </li>
          );
        }
        return loose.includes(box) ? builder(box) : null;
      })}
    </ul>
  );
}
