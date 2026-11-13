/**
 * The tree of environments and namespaces beside the list. Every node is a link to the list's
 * address for that place, so it opens in a new tab too, and keeps the search and the sort.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  type Key,
  Link,
  NavigationTree,
  NavigationTreeItem,
  NavigationTreeItemContent,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { useMe } from "../shell/me";
import { catalogPath } from "./paths";
import { writeChoice } from "./remember";
import { ancestorIds, type CatalogNode, nodeId } from "./tree";
import { type CatalogView, placed } from "./view";

/** Properties of {@link CatalogTree}. */
export interface CatalogTreeProps {
  /** The tree's root, "All tables", whose children are the environments. */
  readonly root: CatalogNode;
  /** The view whose place is marked and whose search and sort every link keeps. */
  readonly view: CatalogView;
}

/**
 * The nodes that start open: every environment, and the places above the view's.
 *
 * @param root - The tree's root.
 * @param view - The view.
 * @returns The ids.
 */
function openAtFirst(root: CatalogNode, view: CatalogView): Set<Key> {
  return new Set<Key>([
    ...root.children.map((environment) => environment.id),
    ...ancestorIds(view.environment, view.namespace),
  ]);
}

/**
 * Renders the tree. Environments start open and namespaces closed; a view that moves deeper
 * opens the places above it and leaves the rest as the person left it.
 *
 * @param props - The root and the view.
 * @param props.root - The tree's root.
 * @param props.view - The view whose place is marked.
 * @returns The tree.
 */
export function CatalogTree({ root, view }: CatalogTreeProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const [expanded, setExpanded] = useState<Set<Key>>(() => openAtFirst(root, view));
  const { environment, namespace } = view;
  // A view that moves deeper opens the places above it; set while rendering, as React advises
  // for state that follows a prop.
  const [shown, setShown] = useState(nodeId(environment, namespace));
  if (shown !== nodeId(environment, namespace)) {
    setShown(nodeId(environment, namespace));
    setExpanded(new Set([...expanded, ...ancestorIds(environment, namespace)]));
  }

  const render = (node: CatalogNode): JSX.Element => {
    const label = node.environment === null ? t("catalog.allTables") : node.label;
    const text = t("catalog.treeNode", { label, count: node.count });
    return (
      <NavigationTreeItem
        key={node.id}
        id={node.id}
        href={catalogPath(placed(view, node.environment, node.namespace))}
        textValue={text}
      >
        <NavigationTreeItemContent>
          {({ isExpanded, hasChildItems }) => (
            <>
              {hasChildItems ? (
                <Button slot="chevron" className="tree-chevron">
                  <span aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
                </Button>
              ) : (
                <span className="tree-chevron" aria-hidden="true" />
              )}
              <Link
                className="tree-link"
                aria-label={text}
                onPress={() => {
                  // Written before the address changes, so that an empty address (All tables)
                  // is never taken for "open the last choice".
                  writeChoice(me.name, {
                    environment: node.environment,
                    namespace: node.namespace,
                  });
                }}
              >
                {label}
                <span className="tree-count" aria-hidden="true">
                  · {node.count}
                </span>
              </Link>
            </>
          )}
        </NavigationTreeItemContent>
        {node.children.map(render)}
      </NavigationTreeItem>
    );
  };

  return (
    <NavigationTree
      className="catalog-tree"
      aria-label={t("catalog.tree")}
      selectedRoute={catalogPath(view)}
      expandedKeys={expanded}
      onExpandedChange={setExpanded}
    >
      {render({ ...root, children: [] })}
      {root.children.map(render)}
    </NavigationTree>
  );
}
