/**
 * The catalog's tree of environments and namespaces, nested from the flat list the server sends.
 *
 * @packageDocumentation
 */
import type { TableTreeOut } from "../api/catalog";

/** One place in the tree. */
export interface CatalogNode {
  /** A stable name for the node, see {@link nodeId}. */
  readonly id: string;
  /** What to show; empty for the root, which the tree shows as "All tables". */
  readonly label: string;
  /** The environment the node stands for; null for the root. */
  readonly environment: string | null;
  /** The dotted namespace; null for the root and an environment. */
  readonly namespace: string | null;
  /** The readable tables at and below the node. */
  readonly count: number;
  /** The places below it. */
  readonly children: readonly CatalogNode[];
}

/**
 * A stable name for a place.
 *
 * @param environment - The environment; null for all.
 * @param namespace - The dotted namespace; null for none.
 * @returns `all`, `env:prod` or `ns:prod:gold.sales`.
 */
export function nodeId(environment: string | null, namespace: string | null): string {
  if (environment === null) {
    return "all";
  }
  return namespace === null ? `env:${environment}` : `ns:${environment}:${namespace}`;
}

/**
 * The nodes above a place, from the environment down, so the tree can open them.
 *
 * @param environment - The environment; null for all.
 * @param namespace - The dotted namespace; null for none.
 * @returns The ids of the environment and of each parent namespace, without the place itself.
 */
export function ancestorIds(
  environment: string | null,
  namespace: string | null,
): readonly string[] {
  if (environment === null || namespace === null) {
    return [];
  }
  const parts = namespace.split(".");
  const parents = parts.slice(1).map((_part, index) => parts.slice(0, index + 1).join("."));
  return [nodeId(environment, null), ...parents.map((path) => nodeId(environment, path))];
}

/**
 * Nests the server's flat list: a namespace goes under its nearest listed ancestor and is
 * labelled with the rest of its path.
 *
 * @param tree - The server's counts.
 * @returns The root, "All tables", with an empty label.
 */
export function buildTree(tree: TableTreeOut): CatalogNode {
  const environments = [...tree.environments].sort((a, b) => a.name.localeCompare(b.name));
  return {
    id: nodeId(null, null),
    label: "",
    environment: null,
    namespace: null,
    count: tree.count,
    children: environments.map((environment) => {
      const paths = environment.namespaces.map((entry) => entry.path);
      const nest = (parent: string | null): CatalogNode[] =>
        environment.namespaces
          .filter((entry) => nearestParent(entry.path, paths) === parent)
          .sort((a, b) => a.path.localeCompare(b.path))
          .map((entry) => ({
            id: nodeId(environment.name, entry.path),
            label: parent === null ? entry.path : entry.path.slice(parent.length + 1),
            environment: environment.name,
            namespace: entry.path,
            count: entry.count,
            children: nest(entry.path),
          }));
      return {
        id: nodeId(environment.name, null),
        label: environment.name,
        environment: environment.name,
        namespace: null,
        count: environment.count,
        children: nest(null),
      };
    }),
  };
}

function nearestParent(path: string, paths: readonly string[]): string | null {
  const parts = path.split(".");
  for (let length = parts.length - 1; length > 0; length -= 1) {
    const candidate = parts.slice(0, length).join(".");
    if (paths.includes(candidate)) {
      return candidate;
    }
  }
  return null;
}

/**
 * Finds the node of a place.
 *
 * @param root - The tree's root.
 * @param environment - The environment; null for the root.
 * @param namespace - The dotted namespace; null for none.
 * @returns The node, or null when the tree does not have the place.
 */
export function findNode(
  root: CatalogNode,
  environment: string | null,
  namespace: string | null,
): CatalogNode | null {
  const id = nodeId(environment, namespace);
  const visit = (node: CatalogNode): CatalogNode | null => {
    if (node.id === id) {
      return node;
    }
    for (const child of node.children) {
      const found = visit(child);
      if (found !== null) {
        return found;
      }
    }
    return null;
  };
  return visit(root);
}
