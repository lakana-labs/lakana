/**
 * What the Overview takes from a lineage answer.
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
