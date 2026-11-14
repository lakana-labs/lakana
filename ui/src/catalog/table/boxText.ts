/**
 * The text of a lineage box as plain lines, how wide it is, and how to cut a line that does not
 * fit, so a box can grow to its longest line and end a longer one in an ellipsis rather than
 * clip it mid-word at its edge.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { useEffect, useState } from "react";

import type { GraphNodeOut } from "../../api";
import { markedText } from "../../shell/VisibleText";
import { splitDotted } from "../paths";
import { jobLabel } from "./layout";
import { kindName } from "./lineageStrings";

/** The space between a box's sides and its text. */
export const TEXT_X = 10;

/** The size of a box's text: `--type-data-size`, which the drawing uses. */
const TEXT_SIZE = "12.5px";

/** The width one character takes on average in the drawing's text, for the estimate. */
const AVERAGE_CHAR = 6.5;

/** How wide a line of a box's text is, in pixels. */
export type Measure = (text: string) => number;

/** What a box shows: its node, and the jobs that run it when it is an asset. */
export interface BoxContent {
  /** The table, asset or connection. */
  readonly node: GraphNodeOut;
  /** The jobs that run an asset; empty for every other box. */
  readonly jobs: readonly GraphNodeOut[];
}

/**
 * A line's width estimated from its length, where no canvas can measure text (a test's DOM).
 *
 * @param text - The line.
 * @returns The width, in pixels.
 */
export function estimateWidth(text: string): number {
  return text.length * AVERAGE_CHAR;
}

/**
 * Measures lines in the drawing's font with a canvas that is never shown, or estimates them
 * where there is none.
 *
 * @returns The measure.
 */
export function textMeasure(): Measure {
  const context =
    typeof OffscreenCanvas === "undefined" ? null : new OffscreenCanvas(1, 1).getContext("2d");
  if (context === null) {
    return estimateWidth;
  }
  const family =
    getComputedStyle(document.documentElement).getPropertyValue("--font-sans").trim() ||
    "sans-serif";
  context.font = `${TEXT_SIZE} ${family}`;
  return (text) => context.measureText(text).width;
}

/**
 * The measure for a drawing, made again once the page's fonts have loaded, so lines measured
 * in a fallback font are measured again in the drawing's own.
 *
 * @returns The measure.
 */
export function useTextMeasure(): Measure {
  const [measure, setMeasure] = useState<Measure>(() => textMeasure());
  useEffect(() => {
    let live = true;
    if ("fonts" in document) {
      void document.fonts.ready.then(() => {
        if (live) {
          setMeasure(() => textMeasure());
        }
      });
    }
    return () => {
      live = false;
    };
  }, []);
  return measure;
}

/**
 * A line cut to the room it has: whole when it fits, else as much as fits followed by "…".
 *
 * @param text - The line.
 * @param room - The room, in pixels.
 * @param measure - How wide a line is.
 * @returns The line, whole or cut.
 */
export function fitText(text: string, room: number, measure: Measure): string {
  if (measure(text) <= room) {
    return text;
  }
  // The longest start that fits with the ellipsis, found by halving.
  let low = 0;
  let high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (measure(`${text.slice(0, middle)}…`) <= room) {
      low = middle;
    } else {
      high = middle - 1;
    }
  }
  return `${text.slice(0, low).trimEnd()}…`;
}

/**
 * A box's two lines as plain text, every invisible character as its mark. A table: its name,
 * then its namespace. An asset: the jobs that run it, then "asset {name} · {repository}". A
 * connection: its name, then its kind.
 *
 * @param t - The translation function.
 * @param box - The box's node and jobs.
 * @returns The first line, and the second or null.
 */
export function boxLines(t: TFunction, box: BoxContent): [string, string | null] {
  const { node } = box;
  if (node.kind === "asset") {
    const first =
      box.jobs.length === 0
        ? t("table.lineage.noJob")
        : `${t("table.lineage.job", { count: box.jobs.length })} ${box.jobs
            .map((job) => markedText(jobLabel(job).job))
            .join(", ")}`;
    const repository = node.repository ?? null;
    const named = t(repository === null ? "table.lineage.asset" : "table.lineage.assetIn").replace(
      "<name/>",
      () => markedText(node.name),
    );
    return [
      first,
      repository === null ? named : named.replace("<repository/>", () => markedText(repository)),
    ];
  }
  if (node.kind === "connection") {
    const kind = kindName(t, node.connection_kind);
    return [markedText(node.name), "word" in kind ? kind.word : markedText(kind.raw)];
  }
  const place = splitDotted(node.name);
  return place === null
    ? [markedText(node.name), null]
    : [markedText(place.name), markedText(place.namespace)];
}

/**
 * How wide a box would be to show its longest line whole.
 *
 * @param t - The translation function.
 * @param box - The box's node and jobs.
 * @param measure - How wide a line is.
 * @returns The width, in pixels.
 */
export function boxWidth(t: TFunction, box: BoxContent, measure: Measure): number {
  const [first, second] = boxLines(t, box);
  return Math.max(measure(first), second === null ? 0 : measure(second)) + 2 * TEXT_X;
}
