/**
 * The body of a tab that reads one thing: the sentence while it reads, the cause with Try again
 * when it fails, and the content once it has arrived.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";

import { useFocusAfter } from "../../shell/useFocusAfter";
import { useResource } from "../../shell/useResource";
import { ReadFailed } from "../cells";

/** Properties of {@link TabBody}. */
export interface TabBodyProps<T> {
  /** The class of the tab's element. */
  readonly className: string;
  /** Reads the data; a new function reads again. */
  readonly load: () => Promise<T>;
  /** What the tab says while it reads. */
  readonly reading: string;
  /** The content, once the data has arrived. */
  readonly children: (data: T) => ReactNode;
}

/**
 * Reads the data and shows what the read gives. After Try again the focus goes to the tab's
 * element, because the button is gone.
 *
 * @param props - The class, the read, the waiting sentence and the content.
 * @param props.className - The class of the tab's element.
 * @param props.load - Reads the data.
 * @param props.reading - What the tab says while it reads.
 * @param props.children - The content.
 * @returns The tab's element.
 */
export function TabBody<T>({ className, load, reading, children }: TabBodyProps<T>): JSX.Element {
  const resource = useResource(load);
  const { target, arm } = useFocusAfter<HTMLDivElement>(resource.data);
  const { data } = resource;
  let shown: ReactNode;
  if (resource.error !== null) {
    shown = (
      <ReadFailed
        message={resource.error}
        onRetry={() => {
          arm();
          resource.reload();
        }}
      />
    );
  } else if (data === undefined) {
    shown = <p role="status">{reading}</p>;
  } else {
    shown = children(data);
  }
  return (
    <div className={className} ref={target} tabIndex={-1}>
      {shown}
    </div>
  );
}
