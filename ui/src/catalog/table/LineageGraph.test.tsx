import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { GraphNodeOut, GraphOut } from "../../api";
import { assetNode, DOWN, graphEdge, graphNode, lineage, ROOT, UP } from "../../test/table";
import { layout } from "./layout";
import { LineageGraph } from "./LineageGraph";

/** Every character the display rule marks, as a pattern; written with escapes only. */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
const RAW = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/u;

/**
 * Draws two lineage answers inside a router.
 *
 * @param up - What feeds the table.
 * @param down - What the table feeds.
 * @returns The drawing.
 */
function draw(up: GraphOut, down: GraphOut = lineage([])): SVGSVGElement {
  const router = createMemoryRouter(
    [{ path: "*", element: <LineageGraph layout={layout(up, down)} environment="prod" /> }],
    { initialEntries: ["/catalog/prod/gold.sales/fact_sales/lineage"] },
  );
  const { container } = render(<RouterProvider router={router} />);
  return container.querySelector("svg") as SVGSVGElement;
}

/**
 * The lines of text of one node's box.
 *
 * @param drawing - The drawing.
 * @param id - The node's id.
 * @returns Each `text` element's content.
 */
function lines(drawing: SVGSVGElement, id: string): (string | null)[] {
  const node = drawing.querySelector(`[data-node="${CSS.escape(id)}"]`);
  if (node === null) {
    throw new Error(`${id} is not drawn`);
  }
  return [...node.querySelectorAll("text")].map((text) => text.textContent);
}

/**
 * The upstream answer with one connection of a kind instead of `pg-sales`.
 *
 * @param kind - The connection's kind.
 * @param name - The connection's name.
 * @returns The answer and the connection's id.
 */
function withConnection(kind: string | null, name = "pg-sales"): { up: GraphOut; id: string } {
  const asset = UP.nodes[1] as GraphNodeOut;
  const connection = graphNode("connection", name, 1, { connection_kind: kind });
  return {
    up: lineage(
      [asset, connection],
      [graphEdge("builds", asset, ROOT), graphEdge("uses", asset, connection)],
    ),
    id: connection.id,
  };
}

describe("LineageGraph", () => {
  it("names the job that runs an asset on its box", () => {
    const drawing = draw(UP, DOWN);

    expect(lines(drawing, "asset:analytics/gold.sales.fact_sales")).toEqual([
      "Job daily_sales",
      "asset gold.sales.fact_sales · analytics",
    ]);
    expect(lines(drawing, "asset:analytics/gold.sales.daily_sales")).toEqual([
      "Run by no job",
      "asset gold.sales.daily_sales · analytics",
    ]);
  });

  it("names every job when several run one asset", () => {
    const asset = UP.nodes[1] as GraphNodeOut;
    const backfill = graphNode("job", "analytics/backfill", 1);
    const up: GraphOut = {
      ...UP,
      nodes: [...UP.nodes, backfill],
      edges: [...UP.edges, graphEdge("runs", backfill, asset)],
    };

    expect(lines(draw(up), asset.id)[0]).toBe("Jobs backfill, daily_sales");
  });

  it("shows a job name without a slash whole", () => {
    const asset = assetNode("gold.sales.fact_sales", 1, "analytics");
    const bare: GraphNodeOut = { ...asset, repository: null };
    const job = graphNode("job", "daily_sales", 1);
    const up = lineage(
      [bare, job],
      [graphEdge("builds", bare, ROOT), graphEdge("runs", job, bare)],
    );

    expect(lines(draw(up), asset.id)).toEqual(["Job daily_sales", "asset gold.sales.fact_sales"]);
  });

  it("draws a connection beside its asset, away from the root", () => {
    const drawing = draw(UP);

    expect(lines(drawing, "connection:pg-sales")).toEqual(["pg-sales", "PostgreSQL"]);
    const { up, id } = withConnection(null, "pg-old");
    expect(lines(draw(up), id)).toEqual(["pg-old", "Connection not set up"]);
  });

  it.each([
    ["postgresql", "PostgreSQL"],
    ["mariadb", "MariaDB"],
    ["sqlite-file", "SQLite file"],
    ["http", "HTTP"],
    ["s3", "S3"],
    ["gcs", "Google Cloud Storage"],
    ["azure", "Azure Blob Storage"],
    ["directory", "Directory"],
    ["tcp", "TCP"],
    ["ftp", "ftp"],
  ])("names every connection kind the server can send: %s", (kind, word) => {
    const { up, id } = withConnection(kind);

    expect(lines(draw(up), id)[1]).toBe(word);
  });

  it("shows an unknown kind as it came, with its invisible characters marked", () => {
    const { up, id } = withConnection("ftp\u202E");

    const drawing = draw(up);

    expect(lines(drawing, id)[1]).toBe("ftpU+202E");
    expect(drawing.querySelector("tspan.char-mark")?.textContent).toBe("U+202E");
  });

  it("draws a connection name as text", () => {
    const { up, id } = withConnection("postgresql", "<b>x</b>");

    const drawing = draw(up);

    expect(lines(drawing, id)[0]).toBe("<b>x</b>");
    expect(drawing.querySelector("b")).toBeNull();
  });

  it("marks a bidi and a control character in an SVG name", () => {
    const strange = graphNode("table", "staging.a\u202Eb\u0007c", 1);
    const asset = UP.nodes[1] as GraphNodeOut;
    const up = lineage(
      [asset, strange],
      [graphEdge("builds", asset, ROOT), graphEdge("reads", asset, strange)],
    );

    const drawing = draw(up);

    const marks = [...drawing.querySelectorAll("tspan.char-mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(["U+202E", "U+0007"]);
    expect(drawing.textContent).not.toMatch(RAW);
  });

  it("names a table by its name and namespace, the table itself apart from the others", () => {
    const drawing = draw(UP, DOWN);

    expect(lines(drawing, ROOT.id)).toEqual(["fact_sales", "gold.sales"]);
    expect(lines(drawing, "table:staging.stg_sales")).toEqual(["stg_sales", "staging"]);
    const root = drawing.querySelector(`[data-node="${CSS.escape(ROOT.id)}"]`);
    expect(root).toHaveClass("lineage-root");
    expect(root?.closest("a")).toBeNull();
  });

  it("links every other table to its page, and nothing else", () => {
    const drawing = draw(UP, DOWN);

    const links = [...drawing.querySelectorAll("a")];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/catalog/prod/staging/stg_sales",
      "/catalog/prod/gold.sales/daily_sales",
    ]);
  });

  it("names a table whose name has no namespace whole, with no link", () => {
    const asset = UP.nodes[1] as GraphNodeOut;
    const bare = graphNode("table", "nodot", 1);
    const up = lineage(
      [asset, bare],
      [graphEdge("builds", asset, ROOT), graphEdge("reads", asset, bare)],
    );

    const drawing = draw(up);

    expect(lines(drawing, bare.id)).toEqual(["nodot"]);
    expect(drawing.querySelector("a")).toBeNull();
  });

  it.each<[string, () => { up: GraphOut; id: string }]>([
    [
      "a job",
      () => {
        const asset = UP.nodes[1] as GraphNodeOut;
        const job = graphNode("job", "analytics/daily\u202Esales", 1);
        const up = lineage(
          [asset, job],
          [graphEdge("builds", asset, ROOT), graphEdge("runs", job, asset)],
        );
        return { up, id: asset.id };
      },
    ],
    [
      "an asset",
      () => {
        const asset = { ...assetNode("gold.sales.fact_sales", 1), name: "gold.fact\u202Esales" };
        return { up: lineage([asset], [graphEdge("builds", asset, ROOT)]), id: asset.id };
      },
    ],
    [
      "a repository",
      () => {
        const asset = assetNode("gold.sales.fact_sales", 1, "analy\u202Etics");
        return { up: lineage([asset], [graphEdge("builds", asset, ROOT)]), id: asset.id };
      },
    ],
    [
      "a namespace",
      () => {
        const asset = UP.nodes[1] as GraphNodeOut;
        const table = graphNode("table", "stag\u202Eing.stg_sales", 1);
        const up = lineage(
          [asset, table],
          [graphEdge("builds", asset, ROOT), graphEdge("reads", asset, table)],
        );
        return { up, id: table.id };
      },
    ],
    ["a connection", () => withConnection("postgresql", "pg\u202Esales")],
  ])("marks a bidi character in %s's name", (_, build) => {
    const { up, id } = build();

    const drawing = draw(up);

    expect(lines(drawing, id).join(" ")).toContain("U+202E");
    expect(drawing.querySelector("text tspan.char-mark")?.textContent).toBe("U+202E");
    expect(drawing.textContent).not.toMatch(RAW);
    const box = drawing.querySelector(`[data-node="${CSS.escape(id)}"]`);
    expect(box?.querySelector("title")?.textContent).toContain("U+202E");
  });

  it("gives every box a title with its whole names, for a name cut at the edge", () => {
    const long = graphNode("table", "staging." + "stg_sales_".repeat(8), 1);
    const asset = UP.nodes[1] as GraphNodeOut;
    const up = lineage(
      [asset, long, ...UP.nodes.slice(3)],
      [graphEdge("builds", asset, ROOT), graphEdge("reads", asset, long), ...UP.edges.slice(2)],
    );

    const drawing = draw(up, DOWN);
    const title = (one: string): string | null | undefined =>
      drawing.querySelector(`[data-node="${CSS.escape(one)}"] > title`)?.textContent;

    expect(title(long.id)).toBe(long.name);
    expect(title(asset.id)).toBe("Job daily_sales\ngold.sales.fact_sales · analytics");
    expect(title("connection:pg-sales")).toBe("pg-sales\nPostgreSQL");
    expect(title("asset:analytics/gold.sales.daily_sales")).toBe(
      "Run by no job\ngold.sales.daily_sales · analytics",
    );
  });

  it("keeps the drawing out of the tab order and the accessibility tree", () => {
    const drawing = draw(UP, DOWN);

    expect(drawing).toHaveAttribute("aria-hidden", "true");
    expect(drawing).toHaveAttribute("focusable", "false");
    for (const link of drawing.querySelectorAll("a")) {
      expect(link).toHaveAttribute("tabindex", "-1");
    }
  });

  it("draws one line per edge between boxes, at the drawing's own size", () => {
    const drawing = draw(UP, DOWN);
    const drawn = layout(UP, DOWN);

    expect(drawing.querySelectorAll("line")).toHaveLength(drawn.edges.length);
    expect(drawing).toHaveAttribute("width", String(drawn.width));
    expect(drawing).toHaveAttribute("height", String(drawn.height));
    expect(drawing).toHaveAttribute(
      "viewBox",
      `0 0 ${String(drawn.width)} ${String(drawn.height)}`,
    );
  });
});
