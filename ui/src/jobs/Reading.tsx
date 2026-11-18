/**
 * The wait of a page of the Jobs screen, while its chunk loads or its object is read: a skeleton,
 * and words after a second.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

import { ListSkeleton } from "../catalog/ListSkeleton";
import { useAfter } from "../queries/useAfter";

/** Properties of {@link Reading}. */
export interface ReadingProps {
  /** What is read, in words, such as "Loading the job…". */
  readonly label: string;
}

/**
 * The skeleton of a page while it is read, and its words once a second has passed.
 *
 * @param props - The words.
 * @param props.label - What is read.
 * @returns The skeleton and the status line.
 */
export function Reading({ label }: ReadingProps): JSX.Element {
  const slow = useAfter();
  return (
    <>
      <p className="jobs-reading" role="status">
        {slow ? label : ""}
      </p>
      <ListSkeleton />
    </>
  );
}
