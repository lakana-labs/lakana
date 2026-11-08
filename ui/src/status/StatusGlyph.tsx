/**
 * The status glyph: a shape per state, so colour is never the only signal.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

import type { StatusKind } from "./status";

const KNOCKOUT = "var(--color-knockout)";

/** Properties of {@link StatusGlyph}. */
export interface StatusGlyphProps {
  /** The state to draw. */
  kind: StatusKind;
  /** Width and height in pixels; 16 by default, 12 is the smallest in use. */
  size?: number;
  /** The word for a glyph that stands alone; without it the glyph is decorative. */
  label?: string;
}

/**
 * Draws the shape of a state: a circle for succeeded, a diamond for failed, a triangle for
 * warning, and rings for the three states that have not produced a result.
 *
 * @param props - The state, the size and an optional label.
 * @param props.kind - The state to draw.
 * @param props.size - Width and height in pixels.
 * @param props.label - The word for a glyph that stands alone.
 * @returns An inline SVG that takes its colour from `currentColor`.
 */
export function StatusGlyph({ kind, size = 16, label }: StatusGlyphProps): JSX.Element {
  const classes = ["status-glyph", `status-glyph-${kind}`];
  if (kind === "running") {
    classes.push("status-glyph-pulse");
  }
  const accessible =
    label === undefined
      ? ({ "aria-hidden": true } as const)
      : ({ role: "img", "aria-label": label } as const);
  return (
    <svg
      className={classes.join(" ")}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      focusable="false"
      {...accessible}
    >
      {SHAPES[kind]}
    </svg>
  );
}

const knockout = { className: "status-glyph-knockout", stroke: KNOCKOUT, strokeWidth: 1.7 };
const ring = { fill: "none", stroke: "currentColor", strokeWidth: 1.5 };

const SHAPES: Record<StatusKind, JSX.Element> = {
  succeeded: (
    <>
      <circle cx="8" cy="8" r="7" fill="currentColor" />
      <path d="M4.8 8.2 7 10.4 11.2 5.8" fill="none" strokeLinecap="round" {...knockout} />
    </>
  ),
  failed: (
    <>
      <path d="M8 1 15 8 8 15 1 8Z" fill="currentColor" />
      <path
        d="M5.8 5.8 10.2 10.2M10.2 5.8 5.8 10.2"
        fill="none"
        strokeLinecap="round"
        {...knockout}
      />
    </>
  ),
  warning: (
    <>
      <path d="M8 1.2 15.2 14 .8 14Z" fill="currentColor" strokeLinejoin="round" />
      <path d="M8 6v3.6M8 11.7v.1" fill="none" strokeLinecap="round" {...knockout} />
    </>
  ),
  running: (
    <>
      <circle cx="8" cy="8" r="6.25" {...ring} />
      <path d="M8 3.5A4.5 4.5 0 0 1 8 12.5Z" fill="currentColor" />
    </>
  ),
  queued: <circle cx="8" cy="8" r="6.25" {...ring} />,
  "upstream-failed": (
    <>
      <circle cx="8" cy="8" r="6.25" {...ring} />
      <path d="M5 8h6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </>
  ),
  cancelled: (
    <>
      <circle cx="8" cy="8" r="6.25" {...ring} />
      <path
        d="M5.4 10.6 10.6 5.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </>
  ),
};
