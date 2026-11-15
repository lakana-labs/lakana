/**
 * The body of a Catalog view other than All, loaded on demand.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

import type { CatalogNode } from "../tree";
import type { CatalogView } from "../view";
import { ChooseEnvironment } from "./ChooseEnvironment";
import { DroppedView } from "./DroppedView";
import { ExpiringView } from "./ExpiringView";
import { OrphansView } from "./OrphansView";

/** Properties of {@link ViewBody}. */
export interface ViewBodyProps {
  /** The view in the address. */
  readonly view: CatalogView;
  /** The tree of places, already read. */
  readonly root: CatalogNode;
}

/**
 * Renders the list of the view: the choice of an environment while there is none, else the
 * list of that environment, or of its namespace.
 *
 * @param props - The view and the tree.
 * @param props.view - The view.
 * @param props.root - The tree of places.
 * @returns The body.
 */
export default function ViewBody({ view, root }: ViewBodyProps): JSX.Element {
  if (view.environment === null) {
    return <ChooseEnvironment view={view} root={root} />;
  }
  if (view.view === "expiring") {
    return <ExpiringView environment={view.environment} namespace={view.namespace} />;
  }
  if (view.view === "orphans") {
    return <OrphansView environment={view.environment} namespace={view.namespace} />;
  }
  return <DroppedView environment={view.environment} namespace={view.namespace} />;
}
