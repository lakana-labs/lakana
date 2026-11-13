/**
 * What the table page takes from a lineage answer.
 *
 * @packageDocumentation
 */
import type { GraphNodeOut, GraphOut } from "../../api";

/**
 * The node the question started from: the one at distance 0.
 *
 * @param graph - The lineage answer.
 * @returns The start, or null when the answer has none.
 */
export function rootOf(graph: GraphOut): GraphNodeOut | null {
  return graph.nodes.find((node) => node.distance === 0) ?? null;
}

/**
 * The tables the answer names besides the start, in the server's order. Jobs, connections and
 * every other kind of node belong to the Lineage tab.
 *
 * @param graph - The lineage answer.
 * @returns The tables.
 */
export function neighbourTables(graph: GraphOut): readonly GraphNodeOut[] {
  const root = rootOf(graph);
  return graph.nodes.filter((node) => node.kind === "table" && node.id !== root?.id);
}

/**
 * One answer from the two ways: each node once, by its id, and each edge once, by its kind,
 * source and target; upstream first. It is cut when either way is.
 *
 * @param up - What feeds the table.
 * @param down - What the table feeds.
 * @returns The answer the Lineage tab draws.
 */
export function mergeGraphs(up: GraphOut, down: GraphOut): GraphOut {
  const nodes = new Map<string, GraphNodeOut>();
  const edges = new Map<string, GraphOut["edges"][number]>();
  for (const graph of [up, down]) {
    for (const node of graph.nodes) {
      if (!nodes.has(node.id)) {
        nodes.set(node.id, node);
      }
    }
    for (const edge of graph.edges) {
      edges.set(JSON.stringify([edge.kind, edge.source, edge.target]), edge);
    }
  }
  return {
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    next_cursor: up.next_cursor ?? down.next_cursor,
  };
}
