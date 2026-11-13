import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PermissionsOut } from "../../api";
import { renderShell } from "../../test/render";
import {
  FILTERS,
  grant,
  GRANTS,
  GRANTS_PATH,
  MASKS,
  permissions,
  rowFilter,
  table,
  TABLE,
  TABLE_PATH,
} from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const TAB = `${PAGE}/access`;
const READER = { name: "kim.schulz", admin: false } as const;
const TABLE_SCOPE = {
  kind: "TABLE",
  environment: "prod",
  namespace: "gold.sales",
  table: "fact_sales",
} as const;

/**
 * The section under a heading of the tab.
 *
 * @param name - The heading's text.
 * @param level - The heading's level.
 * @returns The section.
 */
async function section(name: string, level = 2): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level, name });
  return heading.closest("section") as HTMLElement;
}

/**
 * The cells of a table's rows, as text.
 *
 * @param holder - The element that holds the table.
 * @returns One array of cell texts per body row.
 */
function rows(holder: HTMLElement): string[][] {
  return [...holder.querySelectorAll("tbody tr")].map((row) =>
    [...row.querySelectorAll("td, th")].map(
      // A time's absolute form depends on the time zone; its relative form does not.
      (cell) => cell.querySelector("time")?.textContent ?? cell.textContent,
    ),
  );
}

/** Kim may read the table through a group and write it through a grant; nothing else. */
const KIM_MAY: PermissionsOut = {
  principal: "kim.schulz",
  active: true,
  scope: TABLE_SCOPE,
  permissions: [
    {
      action: "READ_TABLE",
      allowed: true,
      via: [
        {
          kind: "group",
          grant_id: "6d5c4b3a-2f1e-4d0c-9b8a-7f6e5d4c3b03",
          group: "analysts",
          role: "VIEWER",
          scope: TABLE_SCOPE,
        },
      ],
      reason: null,
    },
    {
      action: "WRITE_TABLE",
      allowed: true,
      via: [
        {
          kind: "grant",
          grant_id: "6d5c4b3a-2f1e-4d0c-9b8a-7f6e5d4c3b05",
          group: null,
          role: null,
          scope: { kind: "NAMESPACE", environment: "prod", namespace: "gold", table: null },
        },
      ],
      reason: null,
    },
    { action: "CREATE_TABLE", allowed: false, via: [], reason: null },
    {
      action: "DROP_TABLE",
      allowed: false,
      via: [],
      reason: "No grant on gold.sales.fact_sales gives DROP_TABLE.",
    },
    { action: "MAINTAIN_TABLE", allowed: false, via: [], reason: null },
  ],
  note: null,
};

const KIM_PERMISSIONS = "GET /api/v1/principals/kim.schulz/permissions";

/** A page of 200 grants on every level: the server's largest. */
const MANY = Array.from({ length: 200 }, (_, at) =>
  grant({ id: `6d5c4b3a-2f1e-4d0c-9b8a-${String(at).padStart(12, "0")}` }),
);

describe("AccessTab", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse("2026-11-13T09:30:00Z"), toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says what you may do with the reason for each", async () => {
    renderShell(TAB, {
      me: READER,
      routes: { [KIM_PERMISSIONS]: { status: 200, body: KIM_MAY } },
    });

    const mine = await section("What you may do");
    expect(await within(mine).findAllByRole("row")).toHaveLength(5);
    expect(rows(mine)).toEqual([
      [
        "Read",
        "Allowed",
        "Through the group analysts, which has the Viewer role on table gold.sales.fact_sales in prod.",
      ],
      ["Write", "Allowed", "Through a grant of WRITE_TABLE on namespace gold in prod."],
      ["Drop", "Not allowed", "No grant on gold.sales.fact_sales gives DROP_TABLE."],
      [
        "Edit texts",
        "Not allowed",
        "Only the owner (ana.nowak) or someone who may drop the table may edit its texts. Ask the owner.",
      ],
    ]);
    const glyphs = [...mine.querySelectorAll("tbody .status-glyph")].map((glyph) =>
      glyph.getAttribute("class"),
    );
    expect(glyphs).toEqual([
      "status-glyph status-glyph-succeeded",
      "status-glyph status-glyph-succeeded",
      "status-glyph status-glyph-failed",
      "status-glyph status-glyph-failed",
    ]);
  });

  it("says you may edit texts because you own the table", async () => {
    renderShell(TAB, {
      me: READER,
      routes: {
        [KIM_PERMISSIONS]: { status: 200, body: KIM_MAY },
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ owner: { name: "kim.schulz", kind: "USER" } }),
        },
      },
    });

    const mine = await section("What you may do");
    expect(await within(mine).findByText("You own the table.")).toBeVisible();
  });

  it("tells someone who may not edit texts to ask the owner", async () => {
    renderShell(TAB, {
      me: READER,
      routes: {
        [KIM_PERMISSIONS]: { status: 200, body: permissions("kim.schulz", ["READ_TABLE"]) },
      },
    });

    const mine = await section("What you may do");
    expect(
      await within(mine).findByText(
        "Only the owner (ana.nowak) or someone who may drop the table may edit its texts. Ask the owner.",
      ),
    ).toBeVisible();
  });

  it("names the cause and offers Try again when what you may do cannot be read", async () => {
    let calls = 0;
    const { user } = renderShell(TAB, {
      me: READER,
      routes: {
        [KIM_PERMISSIONS]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The grants store broke." } }
            : { status: 200, body: KIM_MAY };
        },
      },
    });

    const mine = await section("What you may do");
    expect(await within(mine).findByRole("alert")).toHaveTextContent("The grants store broke.");

    await user.click(within(mine).getByRole("button", { name: "Try again" }));

    expect(
      await within(mine).findByText("Through a grant of WRITE_TABLE on namespace gold in prod."),
    ).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "What you may do" })).toHaveFocus();
  });

  it("says Changing access comes with the Access area", async () => {
    renderShell(TAB, { me: READER });

    expect(await screen.findByText("Changing access comes with the Access area.")).toBeVisible();
  });

  it("shows nothing for administrators to a reader and asks nothing", async () => {
    const { server } = renderShell(TAB, { me: READER });

    await within(await section("What you may do")).findAllByRole("row");
    for (const name of ["Who has access", "Row filters", "Column masks"]) {
      expect(screen.queryByRole("heading", { name })).not.toBeInTheDocument();
    }
    const paths = server.sent.map((request) => request.path.split("?")[0]);
    for (const path of [GRANTS_PATH, "/api/v1/row-filters", "/api/v1/column-masks"]) {
      expect(paths).not.toContain(path);
    }
  });

  it("shows administrators who has access at the table, its namespaces and the environment", async () => {
    const { server } = renderShell(TAB);

    const who = await section("Who has access");
    const levels = within(who)
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent);
    expect(levels).toEqual([
      "On this table",
      "On namespace gold.sales",
      "On namespace gold",
      "On environment prod",
    ]);
    await within(who).findAllByText("No grants at this level.");
    expect(within(who).getAllByText("No grants at this level.")).toHaveLength(3);
    expect(
      server.sent.filter((request) => request.path.startsWith(GRANTS_PATH)).map((r) => r.path),
    ).toEqual([
      `${GRANTS_PATH}?environment=prod&namespace=gold.sales&table=fact_sales&limit=200`,
      `${GRANTS_PATH}?environment=prod&namespace=gold.sales&limit=200`,
      `${GRANTS_PATH}?environment=prod&namespace=gold&limit=200`,
      `${GRANTS_PATH}?environment=prod&limit=200`,
    ]);
  });

  it("names the person, service account or group of each grant", async () => {
    renderShell(TAB);

    const level = await section("On this table", 3);
    await within(level).findByRole("link", { name: "kim.schulz" });
    expect(rows(level).slice(0, 3)).toEqual([
      ["kim.schulz", "Analyst role", "1 month ago"],
      ["loaderservice account", "WRITE_TABLE", "1 month ago"],
      ["group analysts", "Viewer role", "1 month ago"],
    ]);
    expect(within(level).getByRole("link", { name: "kim.schulz" })).toHaveAttribute(
      "href",
      "/access/people/kim.schulz",
    );
    expect(within(level).getByRole("link", { name: "loader" })).toHaveAttribute(
      "href",
      "/access/people/loader",
    );
    expect(within(level).getAllByRole("link")).toHaveLength(2);
  });

  it("says a removed person or group for a grant without a subject", async () => {
    renderShell(TAB);

    const level = await section("On this table", 3);
    await within(level).findByRole("link", { name: "kim.schulz" });
    const removed = rows(level)[3];
    expect(removed?.[0]).toBe("a removed person or group 1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c09");
    expect(GRANTS[3]?.principal).toBeNull();
    expect(within(level).queryByRole("link", { name: /removed/ })).not.toBeInTheDocument();
  });

  it("says a removed person or group, by its id, for a policy whose group went while it was read", async () => {
    renderShell(TAB, {
      routes: {
        "GET /api/v1/row-filters": { status: 200, body: [rowFilter({ group: null })] },
      },
    });

    const filters = await section("Row filters");
    await within(filters).findByText("store_id IN (1, 2, 3)");
    expect(rows(filters)).toEqual([
      ["a removed person or group 2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d01", "store_id IN (1, 2, 3)"],
    ]);
  });

  it("marks a bidi character in a grant's subject", async () => {
    renderShell(TAB, {
      routes: {
        [`GET ${GRANTS_PATH}`]: (request) => ({
          status: 200,
          body: request.path.includes("table=")
            ? [grant({ principal_id: null, principal: null, group: "analysts\u202Eeu" })]
            : [],
        }),
      },
    });

    const level = await section("On this table", 3);
    expect(await within(level).findByText("U+202E")).toHaveClass("char-mark");
    expect(level.textContent).not.toContain("\u202E");
  });

  it("names the CLI command per level when a level has more than 200 grants", async () => {
    renderShell(TAB, {
      routes: {
        [`GET ${GRANTS_PATH}`]: {
          status: 200,
          body: MANY,
          headers: { "X-Next-Cursor": "eyJhZnRlciI6IjIwMCJ9" },
        },
      },
    });

    const who = await section("Who has access");
    await within(who).findAllByText(/lists all\.$/);
    const commands = [...who.querySelectorAll(".grants-cut code")].map((code) => code.textContent);
    expect(commands).toEqual([
      "lakana grant list --env 'prod' --ns 'gold.sales' --table 'fact_sales'",
      "lakana grant list --env 'prod' --ns 'gold.sales'",
      "lakana grant list --env 'prod' --ns 'gold'",
      "lakana grant list --env 'prod'",
    ]);
    expect(within(who).getAllByText(/^The first 200 at this level;/)).toHaveLength(4);
  });

  it("says nothing about a first 200 when a level has exactly 200 grants", async () => {
    renderShell(TAB, { routes: { [`GET ${GRANTS_PATH}`]: { status: 200, body: MANY } } });

    const who = await section("Who has access");
    // Plain selectors: role queries over 800 rows are slow.
    await waitFor(() => {
      expect(who.querySelectorAll("tbody tr")).toHaveLength(800);
    });
    expect(who.querySelector(".grants-cut")).toBeNull();
    expect(who.textContent).not.toContain("lists all");
  });

  it("says that grants on the whole instance are not listed, and which command lists them", async () => {
    renderShell(TAB);

    const who = await section("Who has access");
    const line = within(who).getByText(/^Grants on the whole instance, such as the Admin role,/);
    expect(line).toHaveTextContent(
      "Grants on the whole instance, such as the Admin role, are not listed here; lakana grant list shows every grant.",
    );
    expect(line.querySelector("code")).toHaveTextContent("lakana grant list");
  });

  it("marks an invisible character in a level's namespace and environment", async () => {
    const place = "/api/v1/environments/pr\u202Eod/namespaces/gold.sales/tables/fact_sales";
    renderShell("/catalog/pr%E2%80%AEod/gold.sales/fact_sales/access", {
      routes: { [`GET ${place}`]: { status: 200, body: TABLE } },
    });

    const who = await section("Who has access");
    const headings = within(who).getAllByRole("heading", { level: 3 });
    expect(headings.map((heading) => heading.textContent)).toEqual([
      "On this table",
      "On namespace gold.sales",
      "On namespace gold",
      "On environment prU+202Eod",
    ]);
    expect(within(headings[3] as HTMLElement).getByText("U+202E")).toHaveClass("char-mark");
  });

  it("shows row filters without a column and masks with one", async () => {
    renderShell(TAB);

    const filters = await section("Row filters");
    await within(filters).findByText("store_id IN (1, 2, 3)");
    expect(rows(filters)).toEqual([["group analysts", "store_id IN (1, 2, 3)"]]);
    const masks = await section("Column masks");
    await within(masks).findByText("sha256(customer_email)");
    expect(rows(masks)).toEqual([["kim.schulz", "customer_email", "sha256(customer_email)"]]);
    expect(within(masks).getByRole("link", { name: "kim.schulz" })).toHaveAttribute(
      "href",
      "/access/people/kim.schulz",
    );
    expect(within(filters).getByText("store_id IN (1, 2, 3)").closest("code")).not.toBeNull();
    expect(FILTERS).toHaveLength(1);
    expect(MASKS).toHaveLength(1);
  });

  it("shows a mask expression as text", async () => {
    renderShell(TAB, {
      routes: {
        "GET /api/v1/column-masks": {
          status: 200,
          body: [{ ...MASKS[0], expression: "'<b>' || \u202E" }],
        },
      },
    });

    const masks = await section("Column masks");
    const code = (await within(masks).findByText(/'<b>' \|\|/)).closest("code") as HTMLElement;
    expect(code.textContent).toBe("'<b>' || U+202E");
    expect(masks.querySelector("b")).toBeNull();
    expect(within(code).getByText("U+202E")).toHaveClass("char-mark");
  });

  it("does not show whether a policy is bound", async () => {
    renderShell(TAB, {
      routes: {
        "GET /api/v1/row-filters": { status: 200, body: [rowFilter({ bound: false })] },
      },
    });

    const filters = await section("Row filters");
    await within(filters).findByText("store_id IN (1, 2, 3)");
    expect(filters.textContent).not.toMatch(/bound|field id/i);
    expect(
      within(filters)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Person or group", "Rows shown"]);
  });

  it("says when a table has no row filters or masks", async () => {
    renderShell(TAB, {
      routes: {
        "GET /api/v1/row-filters": { status: 200, body: [] },
        "GET /api/v1/column-masks": { status: 200, body: [] },
      },
    });

    expect(
      await within(await section("Row filters")).findByText("No row filters on this table."),
    ).toBeVisible();
    expect(
      await within(await section("Column masks")).findByText("No column masks on this table."),
    ).toBeVisible();
  });

  it("names the cause and offers Try again when a list fails", async () => {
    let calls = 0;
    const { user } = renderShell(TAB, {
      routes: {
        "GET /api/v1/row-filters": () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The policy store broke." } }
            : { status: 200, body: FILTERS };
        },
      },
    });

    const filters = await section("Row filters");
    expect(await within(filters).findByRole("alert")).toHaveTextContent("The policy store broke.");
    expect(
      await within(await section("Column masks")).findByText("sha256(customer_email)"),
    ).toBeVisible();
    expect(
      await within(await section("On this table", 3)).findByRole("link", { name: "loader" }),
    ).toBeVisible();

    await user.click(within(filters).getByRole("button", { name: "Try again" }));

    expect(await within(filters).findByText("store_id IN (1, 2, 3)")).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "Row filters" })).toHaveFocus();
  });

  it("names the cause and offers Try again when one level of grants fails", async () => {
    let calls = 0;
    const { user } = renderShell(TAB, {
      routes: {
        [`GET ${GRANTS_PATH}`]: (request) => {
          if (!request.path.includes("namespace=gold&")) {
            return { status: 200, body: [] };
          }
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The grants store broke." } }
            : { status: 200, body: [grant({ kind: "NAMESPACE", table: null })] };
        },
      },
    });

    const level = await section("On namespace gold", 3);
    expect(await within(level).findByRole("alert")).toHaveTextContent("The grants store broke.");

    await user.click(within(level).getByRole("button", { name: "Try again" }));

    expect(await within(level).findByRole("link", { name: "kim.schulz" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 3, name: "On namespace gold" })).toHaveFocus();
  });
});
