import { screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderShell } from "../../test/render";
import type { FakeServer } from "../../test/server";
import {
  checkResult,
  health,
  MAINTENANCE,
  READERS,
  signalResult,
  table,
  TABLE_PATH,
} from "../../test/table";
import { firedLines } from "./health";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const TAB = `${PAGE}/health`;
const HEALTH_ROUTE = `GET ${TABLE_PATH}/health`;
const READER = { name: "kim.schulz", admin: false } as const;

/**
 * The section under a heading of the tab, once the health has arrived.
 *
 * @param name - The heading's text.
 * @returns The section.
 */
async function section(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 2, name });
  return heading.closest("section") as HTMLElement;
}

/**
 * The cells of a table's rows, as text.
 *
 * @param within_ - The element that holds the table.
 * @returns One array of cell texts per body row.
 */
function rows(within_: HTMLElement): string[][] {
  return [...within_.querySelectorAll("tbody tr")].map((row) =>
    [...row.querySelectorAll("td, th")].map(
      // A time's absolute form depends on the time zone; its relative form does not.
      (cell) => cell.querySelector("time")?.textContent ?? cell.textContent,
    ),
  );
}

/**
 * The health paths the page asked for.
 *
 * @param server - The fake server.
 * @returns The paths.
 */
function healthPaths(server: FakeServer): string[] {
  return server.sent
    .map((request) => request.path)
    .filter((path) => path.split("?")[0]?.endsWith("/health"));
}

describe("HealthTab", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse("2026-11-13T09:30:00Z"), toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks an administrator for the readers", async () => {
    const { server } = renderShell(TAB);

    await section("Readers");
    expect(healthPaths(server)).toEqual([`${TABLE_PATH}/health?days=7`]);
  });

  it("asks everyone else without readers, and never shows a name even if the server sent one", async () => {
    const { server } = renderShell(TAB, {
      me: READER,
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ readers: READERS }) } },
    });

    const readers = await section("Readers");
    expect(healthPaths(server)).toEqual([`${TABLE_PATH}/health?days=7&readers=false`]);
    expect(server.sent.some((request) => request.path.includes("readers=true"))).toBe(false);
    expect(within(readers).getByText("3 people read it in the last 7 days.")).toBeVisible();
    expect(screen.queryByText("kim.schulz")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "ana.nowak" })).not.toBeInTheDocument();
  });

  it("lists every check with its result, failing rows and history line", async () => {
    renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [
              checkResult(),
              checkResult({ name: "amount_positive", outcome: "failed", failing_rows: 4 }),
              checkResult({ name: "fresh_daily", outcome: "warned", failing_rows: 1 }),
              checkResult({ name: "store_exists", outcome: "errored", error: "Timed out." }),
            ],
          }),
        },
      },
    });

    const checks = await section("Checks");
    expect(rows(checks)).toEqual([
      ["unique_sale_id", "Passed", "Not counted", "1 day ago"],
      ["amount_positive", "Failed", "4", "1 day ago"],
      ["fresh_daily", "Warned", "1", "1 day ago"],
      ["store_exists", "Could not runTimed out.", "Not counted", "1 day ago"],
    ]);
    const glyphs = [...checks.querySelectorAll("tbody .status-glyph")].map((glyph) =>
      glyph.getAttribute("class"),
    );
    expect(glyphs).toEqual([
      "status-glyph status-glyph-succeeded",
      "status-glyph status-glyph-failed",
      "status-glyph status-glyph-warning",
      "status-glyph status-glyph-failed",
    ]);
    expect(
      within(checks).getByText("lakana check list --env 'prod' --table 'gold.sales.fact_sales'"),
    ).toBeVisible();
    expect(within(checks).getByRole("button", { name: "Copy command" })).toBeVisible();
  });

  it("marks an invisible character in a check's name", async () => {
    renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({ checks: [checkResult({ name: "unique\u202Eid" })] }),
        },
      },
    });

    const checks = await section("Checks");
    expect(within(checks).getByText("U+202E")).toHaveClass("char-mark");
    expect(checks.textContent).not.toContain("\u202E");
  });

  it("says how many reference checks are not shown", async () => {
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ hidden_checks: 2 }) } },
    });

    expect(
      within(await section("Checks")).getByText(
        "2 reference checks are not shown: their target is a table you may not read.",
      ),
    ).toBeVisible();
  });

  it("says when no check has run", async () => {
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ checks: [] }) } },
    });

    const checks = await section("Checks");
    expect(within(checks).getByText("No check has run on this table yet.")).toBeVisible();
    expect(within(checks).queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists every signal with state, observed value, usual band, effect and last fired", async () => {
    const fired = signalResult({
      state: "fired",
      effect: "block",
      observed: { rows: 120 },
      band: { low: 900, high: 1100 },
      alert: "Rows written fell to 120, below the usual 900 to 1,100.",
      last_fired_at: "2026-11-12T06:06:00Z",
    });
    const quiet = signalResult({
      id: "0c7d7f0e-5c10-4b64-a3f0-5a0b9f6b1002",
      signal: "null_rate",
      column: "customer_email",
      observed: { rate: 0.01, columns: ["a", "b"] },
      band: null,
      effect: "warn",
      last_fired_at: null,
    });
    const answer = health({ signals: [fired, quiet] });
    renderShell(TAB, { routes: { [HEALTH_ROUTE]: { status: 200, body: answer } } });

    const signals = await section("Signals");
    expect(rows(signals)).toEqual([
      [
        "Volume",
        "Whole table",
        "Fired",
        "rows 120",
        "low 900, high 1100",
        "Blocks publication",
        "1 day ago",
      ],
      [
        "Empty values",
        "customer_email",
        "Within its band",
        'rate 0.01, columns ["a","b"]',
        "No usual band yet",
        "Warns",
        "–Never",
      ],
    ]);
    const sentence = firedLines(health({ checks: [], signals: [fired] }), i18next.t)[0]?.sentence;
    expect(sentence).toBe("Rows written fell to 120, below the usual 900 to 1,100.");
    expect(within(signals).getByText(sentence as string)).toBeVisible();
  });

  it("names a signal, a state and an effect the interface does not know by the server's words", async () => {
    renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            signals: [
              signalResult({ signal: "skew", state: "paused", effect: "notify", observed: null }),
              signalResult({
                signal: "freshness",
                state: "no_history",
                effect: "off",
                observed: { source: "history", seconds: 90000 },
              }),
              signalResult({ signal: "schema", state: "no_metrics" }),
              signalResult({ signal: "range", column: "amount", state: "off" }),
            ],
          }),
        },
      },
    });

    const cells = rows(await section("Signals")).map((row) => row.slice(0, 6));
    expect(cells).toEqual([
      ["skew", "Whole table", "paused", "None", "No usual band yet", "notify"],
      [
        "Freshness",
        "Whole table",
        "Not judged yet: too little history",
        "source history, seconds 90000",
        "No usual band yet",
        "Off",
      ],
      [
        "Columns",
        "Whole table",
        "Not judged: no figures recorded",
        "None",
        "No usual band yet",
        "Warns",
      ],
      ["Range", "amount", "Off", "None", "No usual band yet", "Warns"],
    ]);
  });

  it("says when a signal's values are hidden for you", async () => {
    const hidden = signalResult({ state: "fired", values_hidden: true });
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ signals: [hidden] }) } },
    });

    const signals = await section("Signals");
    const [cells] = rows(signals);
    expect(cells?.slice(3, 5)).toEqual(["Hidden for you", "Hidden for you"]);
    expect(
      within(signals).getByText(
        "The number of rows written is outside its usual band. Its values are hidden for you.",
      ),
    ).toBeVisible();
  });

  it("says when no signal watches the table, and when it is not judged yet", async () => {
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ checks: [], signals: [] }) } },
    });

    expect(
      within(await section("Signals")).getByText("No signal watches this table yet."),
    ).toBeVisible();
    expect(screen.getByText("Not judged yet: the table has no checks or signals.")).toBeVisible();
  });

  it("shows the last maintenance", async () => {
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ last_maintenance: MAINTENANCE }) } },
    });

    const maintenance = await section("Last maintenance");
    const facts = [...maintenance.querySelectorAll("dt")].map((term) => [
      term.textContent,
      term.nextElementSibling?.querySelector("time")?.textContent ??
        term.nextElementSibling?.textContent,
    ]);
    expect(facts).toEqual([
      ["Finished", "1 day ago"],
      ["Status", "Complete"],
      ["Snapshots expired", "14"],
      ["Files deleted", "37 (48.2 MB)"],
      ["Tags of failed checks removed", "1"],
    ]);
  });

  it("says a stopped maintenance stopped, and when there was none", async () => {
    renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({ last_maintenance: { ...MAINTENANCE, status: "stopped" } }),
        },
      },
    });

    expect(
      within(await section("Last maintenance")).getByText("Stopped at a safe point"),
    ).toBeVisible();
  });

  it("says when no maintenance changed the table", async () => {
    renderShell(TAB);

    expect(
      within(await section("Last maintenance")).getByText(
        "No maintenance has changed this table yet.",
      ),
    ).toBeVisible();
  });

  it("names the readers with links for an administrator", async () => {
    renderShell(TAB);

    const readers = await section("Readers");
    expect(within(readers).getByRole("link", { name: "kim.schulz" })).toHaveAttribute(
      "href",
      "/access/people/kim.schulz",
    );
    expect(rows(readers)).toEqual([
      ["kim.schulz", "4 reads", "2 hours ago"],
      ["ana.nowak", "1 read", "1 day ago"],
    ]);
    expect(
      within(readers).getByText("Seeing who read the table is recorded in the audit log."),
    ).toBeVisible();
  });

  it("marks an invisible character in a reader's name", async () => {
    renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            readers: [
              { principal: "kim\u202Eschulz", read_count: 4, last_read_at: "2026-11-13T07:30:00Z" },
            ],
          }),
        },
      },
    });

    const readers = await section("Readers");
    expect(within(readers).getByText("U+202E")).toHaveClass("char-mark");
    expect(within(readers).getByRole("link")).toHaveAttribute(
      "href",
      "/access/people/kim%E2%80%AEschulz",
    );
  });

  it("says nobody read the table to an administrator when nobody did", async () => {
    renderShell(TAB, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ readers: [] }) } },
    });

    expect(
      within(await section("Readers")).getByText("Nobody read the table in the last 7 days."),
    ).toBeVisible();
  });

  it("counts the readers without names for everyone else", async () => {
    renderShell(TAB, {
      me: READER,
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ readers_7d: 1 }) } },
    });

    const readers = await section("Readers");
    expect(within(readers).getByText("1 person read it in the last 7 days.")).toBeVisible();
    expect(within(readers).queryByRole("link")).not.toBeInTheDocument();
    expect(within(readers).queryByText(/audit log/)).not.toBeInTheDocument();
  });

  it("counts the readers for an administrator when the server sends no names", async () => {
    renderShell(TAB, { routes: { [HEALTH_ROUTE]: { status: 200, body: health() } } });

    expect(
      within(await section("Readers")).getByText("3 people read it in the last 7 days."),
    ).toBeVisible();
  });

  it("focuses Readers when opened from Readers ›", async () => {
    const { user, router } = renderShell(PAGE);

    await user.click(await screen.findByRole("link", { name: "Readers ›" }));

    const heading = await screen.findByRole("heading", { level: 2, name: "Readers" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(router.state.location.pathname).toBe(TAB);
    expect(router.state.location.hash).toBe("#readers");
    expect(heading.closest("section")).toHaveAttribute("id", "readers");
  });

  it("leaves the focus where it is when opened without #readers", async () => {
    renderShell(TAB);

    const heading = await screen.findByRole("heading", { level: 2, name: "Readers" });
    expect(heading).not.toHaveFocus();
  });

  it("shows the rest of the health when the readers cannot be shown, and says why", async () => {
    const { server } = renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: (request) =>
          request.path.includes("readers=false")
            ? { status: 200, body: health({ checks: [checkResult({ name: "amount_positive" })] }) }
            : {
                status: 503,
                body: {
                  detail:
                    "The readers are not shown because reading them could not be recorded (OperationalError). Try again.",
                },
              },
      },
    });

    const readers = await section("Readers");
    expect(
      within(readers).getByText(
        "Who read the table cannot be shown: seeing it could not be recorded in the audit log. Open the tab again in a moment; if it keeps failing, ask an administrator to check the server.",
      ),
    ).toBeVisible();
    expect(within(readers).getByText("3 people read it in the last 7 days.")).toBeVisible();
    expect(within(await section("Checks")).getByText("amount_positive")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(healthPaths(server)).toEqual([
      `${TABLE_PATH}/health?days=7`,
      `${TABLE_PATH}/health?days=7&readers=false`,
    ]);
  });

  it("names the cause and offers Try again when health cannot be read", async () => {
    let calls = 0;
    const { user } = renderShell(TAB, {
      routes: {
        [HEALTH_ROUTE]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The health store broke." } }
            : { status: 200, body: health({ readers: READERS }) };
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The health store broke.");
    expect(calls).toBe(1);

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: "kim.schulz" })).toBeVisible();
    expect(document.activeElement).not.toBe(document.body);
  });
});
