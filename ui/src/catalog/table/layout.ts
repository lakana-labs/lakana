/**
 * Where the Lineage tab draws each box: a layered layout of the tables and the assets that build
 * them, with the connections at the edges. Pure, with no graph library.
 *
 * @packageDocumentation
 */
import type { GraphNodeOut, GraphOut } from "../../api";
import { mergeGraphs, rootOf } from "./lineage";

/** A box's width, in pixels. */
export const BOX_W = 200;
/** A box's height, in pixels. */
export const BOX_H = 48;
/** The space between two columns, in pixels. */
export const COL_GAP = 64;
/** The space between two rows, in pixels. */
export const ROW_GAP = 16;

/** The most steps the server walks each way. */
export const MOST_STEPS = 10;

/** One box of the drawing. */
export interface LineageBox {
  /** The table, asset or connection it stands for. */
  readonly node: GraphNodeOut;
  /** Its column: 0 for the table, negative upstream, positive downstream. */
  readonly column: number;
  /** Its row in the column, from 0 at the top. */
  readonly row: number;
  /** Its left side. */
  readonly x: number;
  /** Its top side. */
  readonly y: number;
  /** The jobs that run an asset, by name; empty for every other box. */
  readonly jobs: readonly GraphNodeOut[];
}

/** One straight line between two boxes, from the right side of one to the left side of the other. */
interface LineageLine {
  /** Where it starts, across. */
  readonly x1: number;
  /** Where it starts, down. */
  readonly y1: number;
  /** Where it ends, across. */
  readonly x2: number;
  /** Where it ends, down. */
  readonly y2: number;
}

/** The whole drawing. */
export interface LineageLayout {
  /** The boxes, column by column from the left, each column from the top. */
  readonly nodes: readonly LineageBox[];
  /** The lines of the `builds`, `reads` and `uses` edges between drawn boxes. */
  readonly edges: readonly LineageLine[];
  /** The drawing's width. */
  readonly width: number;
  /** The drawing's height. */
  readonly height: number;
}

/** The order of the drawn kinds within a column. */
const KIND_ORDER: Readonly<Record<string, number>> = { table: 0, asset: 1, connection: 2 };

/** A graph's edges, in the form the walk needs. */
type Edge = GraphOut["edges"][number];

/**
 * The columns of the tables and assets one way, by steps along `builds` and `reads`. Upstream, a
 * table leads to the asset that builds it and an asset to the tables it reads; downstream, a
 * table leads to the assets that read it and an asset to the table it builds.
 *
 * @param edges - The answer's edges.
 * @param root - The table's id.
 * @param way - -1 upstream, 1 downstream.
 * @returns Each reached node's column.
 */
function walk(edges: readonly Edge[], root: string, way: -1 | 1): Map<string, number> {
  const [toAsset, toTable] = way === -1 ? ["builds", "reads"] : ["reads", "builds"];
  const columns = new Map([[root, 0]]);
  const queue = [root];
  for (let at = 0; at < queue.length; at += 1) {
    const current = queue[at] as string;
    const column = columns.get(current) as number;
    for (const edge of edges) {
      const next =
        edge.kind === toAsset && edge.target === current
          ? edge.source
          : edge.kind === toTable && edge.source === current
            ? edge.target
            : null;
      if (next !== null && !columns.has(next)) {
        columns.set(next, column + way);
        queue.push(next);
      }
    }
  }
  return columns;
}

/**
 * Lays out what feeds the table and what it feeds. The table is column 0; each step along
 * `builds` and `reads` is one column, upstream to the left and downstream to the right, so
 * assets and tables alternate. A node reached both ways is drawn once, on the side where it is
 * nearer (upstream when as near). A connection is a box one column further from the table than
 * the nearest asset that uses it; a job is not a box but a name on the asset it runs. Each node
 * in a column has its own row (tables, then assets, then connections, each by name), and a
 * column shorter than the tallest sits in its middle. A node no edge reaches is left out.
 *
 * @param up - What feeds the table.
 * @param down - What the table feeds.
 * @returns The boxes, the lines and the size.
 */
export function layout(up: GraphOut, down: GraphOut): LineageLayout {
  const merged = mergeGraphs(up, down);
  const root = rootOf(merged);
  if (root === null) {
    return { nodes: [], edges: [], width: 0, height: 0 };
  }
  const upstream = walk(merged.edges, root.id, -1);
  const downstream = walk(merged.edges, root.id, 1);
  const columns = new Map<string, number>();
  for (const node of merged.nodes) {
    const left = upstream.get(node.id);
    const right = downstream.get(node.id);
    const column = left !== undefined && (right === undefined || -left <= right) ? left : right;
    if (column !== undefined) {
      columns.set(node.id, column);
    }
  }
  const jobs = new Map<string, GraphNodeOut[]>();
  const byId = new Map(merged.nodes.map((node) => [node.id, node]));
  for (const edge of merged.edges) {
    const from = columns.get(edge.source);
    if (edge.kind === "uses" && from !== undefined) {
      const beside = from < 0 ? from - 1 : from + 1;
      const known = columns.get(edge.target);
      if (known === undefined || Math.abs(beside) < Math.abs(known)) {
        columns.set(edge.target, beside);
      }
    }
    const job = byId.get(edge.source);
    if (edge.kind === "runs" && job !== undefined) {
      jobs.set(edge.target, [...(jobs.get(edge.target) ?? []), job]);
    }
  }

  const drawn = merged.nodes
    .filter((node) => columns.has(node.id))
    .map((node) => ({ node, column: columns.get(node.id) as number }))
    .sort(
      (a, b) =>
        a.column - b.column ||
        (KIND_ORDER[a.node.kind] as number) - (KIND_ORDER[b.node.kind] as number) ||
        compare(a.node.name, b.node.name),
    );
  const first = (drawn[0] as (typeof drawn)[number]).column;
  const last = (drawn.at(-1) as (typeof drawn)[number]).column;
  const counts = new Map<number, number>();
  for (const { column } of drawn) {
    counts.set(column, (counts.get(column) ?? 0) + 1);
  }
  const tallest = Math.max(...counts.values());
  const rowStep = BOX_H + ROW_GAP;
  const rows = new Map<number, number>();
  const nodes: LineageBox[] = drawn.map(({ node, column }) => {
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    const offset = ((tallest - (counts.get(column) as number)) * rowStep) / 2;
    return {
      node,
      column,
      row,
      x: (column - first) * (BOX_W + COL_GAP),
      y: offset + row * rowStep,
      jobs: [...(jobs.get(node.id) ?? [])].sort((a, b) => compare(a.name, b.name)),
    };
  });

  const boxes = new Map(nodes.map((box) => [box.node.id, box]));
  const edges: LineageLine[] = [];
  for (const edge of merged.edges) {
    const source = boxes.get(edge.source);
    const target = boxes.get(edge.target);
    if (source !== undefined && target !== undefined) {
      const [left, right] = source.x <= target.x ? [source, target] : [target, source];
      edges.push({
        x1: left.x + BOX_W,
        y1: left.y + BOX_H / 2,
        x2: right.x,
        y2: right.y + BOX_H / 2,
      });
    }
  }
  const across = last - first + 1;
  return {
    nodes,
    edges,
    width: across * BOX_W + (across - 1) * COL_GAP,
    height: tallest * BOX_H + (tallest - 1) * ROW_GAP,
  };
}

/**
 * Orders two names the same way in every browser, whatever the person's language.
 *
 * @param a - One name.
 * @param b - The other.
 * @returns Negative, zero or positive.
 */
function compare(a: string, b: string): number {
  return a.localeCompare(b, "en");
}

/**
 * Reads one depth of the address: a whole number from 1 to {@link MOST_STEPS}; anything else is 1,
 * and anything larger {@link MOST_STEPS}.
 *
 * @param value - The query value, or null without one.
 * @returns The depth.
 */
function readDepth(value: string | null): number {
  if (value === null || !/^\d+$/.test(value)) {
    return 1;
  }
  const depth = Number(value);
  return depth < 1 ? 1 : Math.min(depth, MOST_STEPS);
}

/**
 * Reads the Lineage tab's depths from its address, `?up=2&down=1`.
 *
 * @param params - The address's query.
 * @returns The steps up and down: 1 for a missing or bad value, at most 10.
 */
export function readDepths(params: URLSearchParams): { up: number; down: number } {
  return { up: readDepth(params.get("up")), down: readDepth(params.get("down")) };
}

/**
 * The query that keeps the Lineage tab's depths, with 1 left out.
 *
 * @param depths - The steps up and down.
 * @param depths.up - The steps up.
 * @param depths.down - The steps down.
 * @returns The query, empty at one step each way.
 */
export function depthQuery(depths: { up: number; down: number }): URLSearchParams {
  const query = new URLSearchParams();
  for (const way of ["up", "down"] as const) {
    if (depths[way] > 1) {
      query.set(way, String(depths[way]));
    }
  }
  return query;
}

/**
 * Splits a job's name, `<repository>/<job>`, at its last slash. A job's own name holds no slash
 * (a project refuses one), so the part after it is the job and the part before it the repository.
 *
 * @param node - The job's node.
 * @returns The job's name, and its repository or null when the name has no slash.
 */
export function jobLabel(node: GraphNodeOut): { job: string; repository: string | null } {
  const slash = node.name.lastIndexOf("/");
  return slash < 0
    ? { job: node.name, repository: null }
    : { job: node.name.slice(slash + 1), repository: node.name.slice(0, slash) };
}
