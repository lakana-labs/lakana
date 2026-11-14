import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FakeServer } from "../../test/server";
import { renderShell } from "../../test/render";
import {
  permissions,
  QUERIES_PATH,
  QUERY_ID,
  queryStatus,
  result,
  table,
  TABLE_PATH,
  VIEW,
  VIEW_RESULT,
} from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const PREVIEW = `${PAGE}/preview`;
const RESULT_ROUTE = `GET ${QUERIES_PATH}/${QUERY_ID}/result`;
const STATUS_ROUTE = `GET ${QUERIES_PATH}/${QUERY_ID}`;

/** Every character the display rule marks, as a pattern; written with escapes only. */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
const RAW = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/u;

/**
 * The paths of the requests sent with a method.
 *
 * @param server - The fake server.
 * @param method - The method.
 * @param part - The end of the path.
 * @returns The paths.
 */
function sent(server: FakeServer, method: string, part: string): string[] {
  return server.sent
    .filter((request) => request.method === method && request.path.endsWith(part))
    .map((request) => request.path);
}

/**
 * The preview's grid, once its rows arrived.
 *
 * @returns The grid.
 */
function grid(): Promise<HTMLElement> {
  return screen.findByRole("grid", { name: "The first rows of fact_sales" });
}

/**
 * The text of the preview's line, once the page arrived.
 *
 * @returns The line.
 */
async function line(): Promise<HTMLElement> {
  await grid();
  return screen.getByRole("status");
}

/**
 * The cells of one row of the grid.
 *
 * @param table - The grid.
 * @param index - The row, 1 for the first row of data.
 * @returns The cells' text.
 */
function rowText(table: HTMLElement, index: number): (string | null)[] {
  const row = within(table).getAllByRole("row")[index] as HTMLElement;
  return Array.from(row.querySelectorAll("td, th"), (cell) => cell.textContent);
}

/** A fake server's answer: a status and a JSON body. */
interface Reply {
  /** The status. */
  readonly status: number;
  /** The body. */
  readonly body: unknown;
}

/** Lets every answer the fake server gave reach the page, without moving the clock. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 20; round += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  });
}

/**
 * Lets answers and modules loaded on demand arrive until a condition holds, for at most 5 s of
 * real time; the fake clock does not move.
 *
 * @param done - The condition.
 */
async function settleUntil(done: () => boolean): Promise<void> {
  const until = Date.now() + 5_000;
  while (!done() && Date.now() < until) {
    await settle();
  }
}

/**
 * Moves the clock on, then lets the answers arrive.
 *
 * @param ms - The milliseconds.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

describe("PreviewTab", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("runs only when the tab is opened", async () => {
    const { server, user } = renderShell(PAGE);
    await screen.findByText("Every sale in every store, one row per sale.");
    expect(sent(server, "POST", "/preview")).toEqual([]);

    await user.click(screen.getByRole("link", { name: "Preview" }));

    await grid();
    expect(sent(server, "POST", "/preview")).toEqual([`${TABLE_PATH}/preview`]);
    expect(sent(server, "GET", "/permissions?environment=prod")).toEqual([
      "/api/v1/principals/admin/permissions?environment=prod",
    ]);
  });

  it("shows up to 100 rows read as you with the time", async () => {
    const rows = Array.from({ length: 100 }, (_, index) => [String(index), "****", "", null, null]);
    renderShell(PREVIEW, {
      routes: { [RESULT_ROUTE]: { status: 200, body: result({ rows, row_count: 100 }) } },
    });

    expect(await line()).toHaveTextContent("First 100 rows · read as you · 0.8 s");
    expect(within(await grid()).getAllByRole("row")).toHaveLength(101);
  });

  it("says how many rows there are, and leaves out an unknown time", async () => {
    renderShell(PREVIEW);
    expect(await line()).toHaveTextContent(/^2 rows · read as you · 0\.8 s$/);
  });

  it("leaves out the time when the server does not know it", async () => {
    renderShell(PREVIEW, {
      routes: { [RESULT_ROUTE]: { status: 200, body: result({ duration_ms: null }) } },
    });
    expect(await line()).toHaveTextContent(/^2 rows · read as you$/);
  });

  it("says when the rows were too wide to send them all", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: { status: 200, body: result({ row_count: 100, next_offset: 2 }) },
      },
    });
    expect(await line()).toHaveTextContent("First 2 of 100 rows · read as you · 0.8 s");
  });

  it("names the columns masked for the viewer and nothing else", async () => {
    renderShell(PREVIEW);
    await grid();

    const masked = screen.getByText("Masked for you:").parentElement as HTMLElement;
    expect(masked).toHaveTextContent(/^Masked for you: customer_email$/);
  });

  it("says how many more columns are masked than the page names", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: {
          status: 200,
          body: result({
            masked_columns: [
              { table: "gold.sales.fact_sales", column: "customer_email" },
              { table: "gold.sales.fact_sales", column: "note" },
            ],
            masked_columns_cut: 3,
          }),
        },
      },
    });
    await grid();

    const masked = screen.getByText("Masked for you:").parentElement as HTMLElement;
    expect(masked).toHaveTextContent(/^Masked for you: customer_email, note and 3 more$/);
  });

  it("says nothing of masks when none was applied or none was recorded", async () => {
    renderShell(PREVIEW, {
      routes: { [RESULT_ROUTE]: { status: 200, body: result({ masked_columns: null }) } },
    });
    await grid();
    expect(screen.queryByText("Masked for you:")).not.toBeInTheDocument();
  });

  it("previews a view and names a base table's masked column", async () => {
    const { server } = renderShell(PREVIEW, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: VIEW },
        [RESULT_ROUTE]: { status: 200, body: VIEW_RESULT },
      },
    });
    await grid();

    expect(sent(server, "POST", "/preview")).toEqual([`${TABLE_PATH}/preview`]);
    const masked = screen.getByText("Masked for you:").parentElement as HTMLElement;
    expect(masked).toHaveTextContent(/^Masked for you: column email of gold\.orders$/);
  });

  it("shows a viewer one line with the cause and the next step and sends no preview", async () => {
    const { server } = renderShell(PREVIEW, {
      routes: {
        "GET /api/v1/principals/admin/permissions": {
          status: 200,
          body: permissions("admin", ["READ_TABLE"]),
        },
      },
    });

    expect(
      await screen.findByText(
        "Preview runs a query as you, which needs the right to run queries in prod. Ask an administrator for the Analyst role there.",
      ),
    ).toBeVisible();
    expect(sent(server, "POST", "/preview")).toEqual([]);
    expect(screen.queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
  });

  it("shows the server's sentence when the preview is refused", async () => {
    renderShell(PREVIEW, {
      routes: {
        [`POST ${TABLE_PATH}/preview`]: {
          status: 403,
          body: { detail: "You may not run queries here.", type: "PermissionDenied" },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("You may not run queries here.");
  });

  it("says an artifact has no preview and asks nothing", async () => {
    const { server } = renderShell(PREVIEW, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ kind: "ARTIFACT" }) } },
    });

    expect(
      await screen.findByText(
        "An artifact holds files, not rows, so it has no preview. Download its files with lakana artifact download.",
      ),
    ).toBeVisible();
    expect(sent(server, "GET", "/permissions?environment=prod")).toEqual([]);
    expect(sent(server, "POST", "/preview")).toEqual([]);
    expect(server.sent.filter((request) => request.path.includes("/queries"))).toEqual([]);
  });

  it("waits with words after a second", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    renderShell(PREVIEW, {
      routes: { [STATUS_ROUTE]: { status: 200, body: queryStatus("QUEUED") } },
    });
    await settleUntil(() => screen.queryByText("Queued") !== null);
    expect(screen.getByText("Queued")).toBeVisible();

    await advance(999);
    expect(screen.queryByText("Waiting for a worker…")).not.toBeInTheDocument();
    await advance(1);
    expect(screen.getByText("Waiting for a worker…")).toBeVisible();
  });

  it("says each step before the rows in its own words", async () => {
    const gates = new Map<string, () => void>();
    /**
     * An answer that arrives only once its gate is opened.
     *
     * @param name - The gate.
     * @param reply - The answer.
     * @returns The answer, once the gate is opened.
     */
    const gated = (name: string, reply: Reply): Promise<Reply> =>
      new Promise((resolve) => {
        gates.set(name, () => {
          resolve(reply);
        });
      });
    renderShell(PREVIEW, {
      routes: {
        // The table's own permissions answer at once; the environment's wait for the gate.
        "GET /api/v1/principals/admin/permissions": (request) => {
          const reply = { status: 200, body: permissions("admin", ["READ_TABLE", "SUBMIT_QUERY"]) };
          return request.path.includes("table=") ? reply : gated("rights", reply);
        },
        [`POST ${TABLE_PATH}/preview`]: () =>
          gated("queue", {
            status: 202,
            body: { query_id: QUERY_ID },
          }),
        [RESULT_ROUTE]: () => gated("read", { status: 200, body: result() }),
      },
    });
    const line = async (words: string): Promise<void> => {
      await waitFor(() => {
        expect(screen.getByRole("status")).toHaveTextContent(new RegExp(`^${words}$`));
      });
    };

    await waitFor(() => {
      expect(gates.has("rights")).toBe(true);
    });
    await line("Checking that you may run queries…");
    gates.get("rights")?.();
    await waitFor(() => {
      expect(gates.has("queue")).toBe(true);
    });
    await line("Queuing the preview…");
    gates.get("queue")?.();
    await line("Reading the rows…");
    gates.get("read")?.();
    await line("2 rows · read as you · 0.8 s");
  });

  it("says a running preview is running", async () => {
    renderShell(PREVIEW, {
      routes: { [STATUS_ROUTE]: { status: 200, body: queryStatus("RUNNING") } },
    });
    expect(await screen.findByText("Running")).toBeVisible();
  });

  it("cancels the run when the tab is left", async () => {
    const { server, user } = renderShell(PREVIEW, {
      routes: { [STATUS_ROUTE]: { status: 200, body: queryStatus("QUEUED") } },
    });
    await screen.findByText("Queued");

    await user.click(screen.getByRole("link", { name: "Overview" }));

    await waitFor(() => {
      expect(sent(server, "POST", "/cancel")).toEqual([`${QUERIES_PATH}/${QUERY_ID}/cancel`]);
    });
  });

  it("reruns with Refresh and moves focus to Refresh after it", async () => {
    const { server, user } = renderShell(PREVIEW);
    await grid();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(sent(server, "POST", "/preview")).toHaveLength(2);
    });
    await grid();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
  });

  it("says when the table has no rows", async () => {
    renderShell(PREVIEW, {
      routes: { [RESULT_ROUTE]: { status: 200, body: result({ rows: [], row_count: 0 }) } },
    });

    expect(
      await screen.findByText("No rows to show. Overview says when the table was last written."),
    ).toBeVisible();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
  });

  it("shows the failure's sentence and offers Refresh", async () => {
    renderShell(PREVIEW, {
      routes: {
        [STATUS_ROUTE]: {
          status: 200,
          body: queryStatus("FAILED", {
            error_message: "The table changed since the preview was asked for; refresh.",
          }),
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The table changed since the preview was asked for; refresh.",
    );
    expect(screen.getByRole("button", { name: "Refresh" })).toBeVisible();
  });

  it("marks an invisible character in a failed run's message", async () => {
    renderShell(PREVIEW, {
      routes: {
        [STATUS_ROUTE]: {
          status: 200,
          body: queryStatus("FAILED", { error_message: "Cannot cast 'ab\u202Ecd' to INTEGER." }),
        },
      },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Cannot cast 'ab");
    expect(alert.textContent).not.toContain("\u202E");
    expect(alert.querySelector(".char-mark")).not.toBeNull();
  });

  it("cuts long values and shows one whole on focus or click", async () => {
    const long = "a".repeat(500);
    const { user } = renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: { status: 200, body: result({ rows: [["1", "****", long, null, null]] }) },
      },
    });
    const table = await grid();
    expect(
      screen.getByText(
        "Move to a cell with the arrow keys, or click one, to see its whole value here.",
      ),
    ).toBeVisible();

    await user.click(within(table).getByText(long));

    const panel = screen.getByRole("region", { name: "note, row 1" });
    expect(panel).toHaveTextContent(long);
  });

  it("is one tab stop and moves between cells with the arrow keys", async () => {
    const { user } = renderShell(PREVIEW);
    const table = await grid();
    screen.getByRole("button", { name: "Refresh" }).focus();

    await user.tab();
    expect(table.contains(document.activeElement)).toBe(true);
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("region", { name: /^customer_email, row 1$/ })).toBeVisible();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("region", { name: /^customer_email, row 2$/ })).toBeVisible();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("region", { name: "note, row 2" })).toHaveTextContent("null");

    await user.tab();
    expect(table.contains(document.activeElement)).toBe(false);
    await user.tab({ shift: true });
    expect(table.contains(document.activeElement)).toBe(true);
  });

  it("marks a bidi character in a cell", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: {
          status: 200,
          body: result({
            columns: [{ name: "n\u202Eote", type: "string" }],
            rows: [["x\u202Ey"]],
          }),
        },
      },
    });
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["xU+202Ey"]);
    expect(within(table).getAllByRole("columnheader")[0]).toHaveTextContent("nU+202Eote");
    expect(RAW.test(table.textContent)).toBe(false);
  });

  it("marks the cells the server left out of a cut row as cut", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: {
          status: 200,
          body: result({ rows: [["1", "****"]], row_cut: [0], columns_cut: 3 }),
        },
      },
    });
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "… (cut)", "… (cut)", "… (cut)"]);
  });

  it("marks a struct cell the server cut, and says so", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: {
          status: 200,
          body: result({ rows: [["1", "****", "a", null, { cut: true }]] }),
        },
      },
    });
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "a", "null", "… (cut)"]);
    expect(
      screen.getByText("Some values were too large to send and are marked cut."),
    ).toBeVisible();
  });

  it("marks cut values and rows", async () => {
    renderShell(PREVIEW, {
      routes: {
        [RESULT_ROUTE]: {
          status: 200,
          body: result({
            rows: [["1", "****", { cut: true, text: "abc" }, { cut: true }, { cut: true }]],
            row_cut: [0],
            columns_cut: 2,
          }),
        },
      },
    });
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "abc… (cut)", "… (cut)", "… (cut)"]);
    expect(
      screen.getByText("Some values were too large to send and are marked cut."),
    ).toBeVisible();
  });

  it("writes binary, null and struct cells as text", async () => {
    renderShell(PREVIEW);
    const table = await grid();

    expect(rowText(table, 1)).toEqual([
      "1",
      "****",
      "xU+202Ey",
      "binary, 12 bytes",
      '{"binary":3,"cut":true}',
    ]);
    expect(rowText(table, 2)).toEqual(["2", "****", "null", "null", "null"]);
  });
});
