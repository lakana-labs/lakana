/**
 * The Lakana mark: the outrigger canoe of the logo, flat and in one colour.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * The mark's outline on a 16 × 16 grid: the sail, then the hull with its outrigger boom and float.
 * The same path is in `ui/src/shell/lakana-mark.svg` and `public/favicon.svg`
 * (`LakanaMark.test.tsx` keeps them equal).
 */
const OUTLINE = "M5 2Q10.5 4.5 13 9H6.5ZM1 11H12.6L15.5 9.4 13.4 14H7.4L6.6 12H3V13H1Z";

/** Properties of {@link LakanaMark}. */
export interface LakanaMarkProps {
  /** Width and height in pixels; 16 by default, the smallest it is drawn at. */
  readonly size?: number;
  /** The name for a mark that stands alone; without it the mark is decorative. */
  readonly label?: string;
}

/**
 * Draws the mark in `currentColor`.
 *
 * @param props - The size and an optional label.
 * @param props.size - Width and height in pixels.
 * @param props.label - The name for a mark that stands alone.
 * @returns An inline SVG.
 */
export function LakanaMark({ size = 16, label }: LakanaMarkProps): JSX.Element {
  const accessible =
    label === undefined
      ? ({ "aria-hidden": true } as const)
      : ({ role: "img", "aria-label": label } as const);
  return (
    <svg
      className="lakana-mark"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      focusable="false"
      {...accessible}
    >
      <path fill="currentColor" d={OUTLINE} />
    </svg>
  );
}
