/**
 * The Lineage tab's drawing: one box per table, asset and connection, and straight lines
 * between them. It is a picture for the eye only; the list beside it says the same in words.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import type { TFunction } from "i18next";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { GraphNodeOut } from "../../api";
import { markedText, VisibleTspan } from "../../shell/VisibleText";
import { splitDotted, tablePath } from "../paths";
import { BOX_H, BOX_W, jobLabel, type LineageBox, type LineageLayout } from "./layout";
import { kindName } from "./lineageStrings";

/** Properties of {@link LineageGraph}. */
export interface LineageGraphProps {
  /** Where each box goes. */
  readonly layout: LineageLayout;
  /** The environment, for the links of the tables. */
  readonly environment: string;
  /** A class for the drawing, such as the full-screen view's. */
  readonly className?: string;
}

/** Where a box's first line of text sits, down from its top. */
const FIRST_LINE = 20;
/** Where a box's second line of text sits. */
const SECOND_LINE = 38;
/** The space between a box's left side and its text. */
const TEXT_X = 10;

/** Properties of {@link BoxText}. */
interface BoxTextProps {
  /** The box. */
  readonly box: LineageBox;
}

/**
 * The two lines of a box. A table: its name, then its namespace. An asset: the jobs that run
 * it, then "asset {name} · {repository}". A connection: its name, then its kind.
 *
 * @param props - The box.
 * @param props.box - The box.
 * @returns The lines, as SVG text.
 */
function BoxText({ box }: BoxTextProps): JSX.Element {
  const { t } = useTranslation();
  const { node } = box;
  let first: JSX.Element;
  let second: JSX.Element | null;
  if (node.kind === "asset") {
    first =
      box.jobs.length === 0 ? (
        <>{t("table.lineage.noJob")}</>
      ) : (
        <>
          {t("table.lineage.job", { count: box.jobs.length })}{" "}
          {box.jobs.map((job, index) => (
            <tspan key={job.id}>
              {index > 0 && ", "}
              <VisibleTspan text={jobLabel(job).job} />
            </tspan>
          ))}
        </>
      );
    const repository = node.repository ?? null;
    second = (
      <Trans
        t={t}
        i18nKey={repository === null ? "table.lineage.asset" : "table.lineage.assetIn"}
        components={{
          name: <VisibleTspan text={node.name} />,
          repository: <VisibleTspan text={repository ?? ""} />,
        }}
      />
    );
  } else if (node.kind === "connection") {
    const kind = kindName(t, node.connection_kind);
    first = <VisibleTspan text={node.name} />;
    second = "word" in kind ? <>{kind.word}</> : <VisibleTspan text={kind.raw} />;
  } else {
    const place = splitDotted(node.name);
    first = <VisibleTspan text={place === null ? node.name : place.name} />;
    second = place === null ? null : <VisibleTspan text={place.namespace} />;
  }
  return (
    <>
      <text x={TEXT_X} y={FIRST_LINE} className="lineage-first">
        {first}
      </text>
      {second !== null && (
        <text x={TEXT_X} y={SECOND_LINE} className="lineage-second">
          {second}
        </text>
      )}
    </>
  );
}

/**
 * A box's title, shown on hover: its names whole, one per line, which the box may cut at its
 * edge. Plain text with every invisible character as its mark, never the character itself.
 *
 * @param t - The translation function.
 * @param box - The box.
 * @returns The title.
 */
function boxTitle(t: TFunction, box: LineageBox): string {
  const { node } = box;
  let lines: string[];
  if (node.kind === "asset") {
    const jobs = box.jobs.map((job) => markedText(jobLabel(job).job)).join(", ");
    const asset = [node.name, node.repository ?? null]
      .filter((part) => part !== null)
      .map(markedText)
      .join(" · ");
    lines = [
      box.jobs.length === 0
        ? t("table.lineage.noJob")
        : `${t("table.lineage.job", { count: box.jobs.length })} ${jobs}`,
      asset,
    ];
  } else if (node.kind === "connection") {
    const kind = kindName(t, node.connection_kind);
    lines = [markedText(node.name), "word" in kind ? kind.word : markedText(kind.raw)];
  } else {
    lines = [markedText(node.name)];
  }
  return lines.join("\n");
}

/**
 * The page a table's box opens, or null for the table itself and a name that is no table's.
 *
 * @param node - The box's node.
 * @param environment - The environment.
 * @returns The path, or null.
 */
function boxPath(node: GraphNodeOut, environment: string): string | null {
  const place = node.kind === "table" && node.distance > 0 ? splitDotted(node.name) : null;
  return place === null ? null : tablePath(environment, place.namespace, place.name);
}

/**
 * Draws the boxes and lines at their own size, hidden from assistive technology and out of the
 * tab order: the list beside it carries the same nodes and edges. A table's box is a link to its
 * page: a plain click opens it in place through the router, and a click with a modifier key, a
 * middle click or "copy link" are the browser's. Jobs and connections are not links. Every name
 * is text, with invisible characters marked; each box has a title with its whole names, so a name
 * cut at the box's edge can be read on hover.
 *
 * @param props - The layout and the environment.
 * @param props.layout - Where each box goes.
 * @param props.environment - The environment.
 * @param props.className - A class for the drawing.
 * @returns The drawing.
 */
export function LineageGraph({
  layout,
  environment,
  className = "lineage-graph",
}: LineageGraphProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <svg
      className={className}
      width={layout.width}
      height={layout.height}
      viewBox={`0 0 ${String(layout.width)} ${String(layout.height)}`}
      aria-hidden="true"
      focusable="false"
    >
      {layout.edges.map((line, index) => (
        <line
          // Two edges may share a line; the layout never reorders them.
          key={index}
          className="lineage-edge"
          {...line}
        />
      ))}
      {layout.nodes.map((box) => {
        const root = box.node.distance === 0 && box.node.kind === "table";
        const drawn = (
          <svg
            x={box.x}
            y={box.y}
            width={BOX_W}
            height={BOX_H}
            data-node={box.node.id}
            className={`lineage-box lineage-${box.node.kind}${root ? " lineage-root" : ""}`}
          >
            <title>{boxTitle(t, box)}</title>
            <rect x={0.5} y={0.5} width={BOX_W - 1} height={BOX_H - 1} rx={4} />
            <BoxText box={box} />
          </svg>
        );
        const path = boxPath(box.node, environment);
        return path === null ? (
          <g key={box.node.id}>{drawn}</g>
        ) : (
          <Link
            key={box.node.id}
            to={path}
            // Out of the tab order: keyboard users open tables from the list beside the drawing.
            tabIndex={-1}
          >
            {drawn}
          </Link>
        );
      })}
    </svg>
  );
}
