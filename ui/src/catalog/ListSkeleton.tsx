/**
 * The skeleton rows shown while a list is read.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

const SKELETON_ROWS = 5;

/**
 * Renders the rows, hidden from assistive technology.
 *
 * @returns The skeleton.
 */
export function ListSkeleton(): JSX.Element {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: SKELETON_ROWS }, (_row, index) => (
        <div className="skeleton-row" key={index} />
      ))}
    </div>
  );
}
