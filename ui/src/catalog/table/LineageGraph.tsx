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
import { markedName, markedText, VisibleTspan } from "../../shell/VisibleText";
import { splitDotted, tablePath } from "../paths";
import { boxLines, estimateWidth, fitText, type Measure, TEXT_X } from "./boxText";
import { BOX_H, jobLabel, type LineageBox, type LineageLayout } from "./layout";
import { kindName } from "./lineageStrings";

/** Properties of {@link LineageGraph}. */
export interface LineageGraphProps {
  /** Where each box goes. */
  readonly layout: LineageLayout;
  /** The environment, for the links of the tables. */
  readonly environment: string;
  /** A class for the drawing, such as the full-screen view's. */
  readonly className?: string;
  /** How wide a line of text is, to cut one longer than its box; estimated when left out. */
  readonly measure?: Measure;
}

/** Where a box's first line of text sits, down from its top. */
const FIRST_LINE = 20;
/** Where a box's second line of text sits. */
const SECOND_LINE = 38;
/** Properties of {@link BoxText}. */
interface BoxTextProps {
  /** The box. */
  readonly box: LineageBox;
  /** How wide a line of text is. */
  readonly measure: Measure;
}

/**
 * The two lines of a box. A table: its name, then its namespace. An asset: the jobs that run
 * it, then "asset {name} · {repository}". A connection: its name, then its kind. A line longer
 * than the box ends in "…" (the box's title has it whole), with its invisible characters as
 * plain marks.
 *
 * @param props - The box and the measure.
 * @param props.box - The box.
 * @param props.measure - How wide a line of text is.
 * @returns The lines, as SVG text.
 */
function BoxText({ box, measure }: BoxTextProps): JSX.Element {
  const { t } = useTranslation();
  const { node } = box;
  const room = box.width - 2 * TEXT_X;
  const plain = boxLines(t, box);
  const fitted = plain.map((line) => (line === null ? null : fitText(line, room, measure)));
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
          name: <VisibleTspan text={node.name} name />,
          repository: <VisibleTspan text={repository ?? ""} />,
        }}
      />
    );
  } else if (node.kind === "connection") {
    const kind = kindName(t, node.connection_kind);
    first = <VisibleTspan text={node.name} name />;
    second = "word" in kind ? <>{kind.word}</> : <VisibleTspan text={kind.raw} />;
  } else {
    const place = splitDotted(node.name);
    first = <VisibleTspan text={place === null ? node.name : place.name} name />;
    second = place === null ? null : <VisibleTspan text={place.namespace} name />;
  }
  const [firstFit, secondFit] = fitted;
  return (
    <>
      <text x={TEXT_X} y={FIRST_LINE} className="lineage-first">
        {firstFit === plain[0] ? first : firstFit}
      </text>
      {second !== null && (
        <text x={TEXT_X} y={SECOND_LINE} className="lineage-second">
          {secondFit === plain[1] ? second : secondFit}
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
    lines = [markedName(node.name), "word" in kind ? kind.word : markedText(kind.raw)];
  } else {
    lines = [markedName(node.name)];
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
 * is text, with invisible characters marked. A box is as wide as its column, which grows to its
 * longest line up to 320 px; a longer line ends in "…", and each box has a title with its whole
 * names, so a cut name can be read on hover.
 *
 * @param props - The layout and the environment.
 * @param props.layout - Where each box goes.
 * @param props.environment - The environment.
 * @param props.className - A class for the drawing.
 * @param props.measure - How wide a line of text is.
 * @returns The drawing.
 */
export function LineageGraph({
  layout,
  environment,
  className = "lineage-graph",
  measure = estimateWidth,
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
            width={box.width}
            height={BOX_H}
            data-node={box.node.id}
            className={`lineage-box lineage-${box.node.kind}${root ? " lineage-root" : ""}`}
          >
            <title>{boxTitle(t, box)}</title>
            <rect x={0.5} y={0.5} width={box.width - 1} height={BOX_H - 1} rx={4} />
            <BoxText box={box} measure={measure} />
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
