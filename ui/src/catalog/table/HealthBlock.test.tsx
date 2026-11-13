import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { checkResult, health, signalResult, table, TABLE_PATH } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const HEALTH_ROUTE = `GET ${TABLE_PATH}/health`;

/**
 * The health block, once the page has arrived.
 *
 * @returns The block's element.
 */
async function block(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 2, name: "Health" });
  return heading.parentElement as HTMLElement;
}

describe("HealthBlock", () => {
  it("takes the headline word from the table's health summary, whatever the lines say", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({
            health_summary: {
              state: "warning",
              item: "rows_dropped",
              text: "Rows dropped",
              others: 0,
            },
          }),
        },
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({ checks: [checkResult({ outcome: "failed", failing_rows: 4 })] }),
        },
      },
    });

    const health_ = await block();
    expect(await within(health_).findByText("Rows dropped")).toBeVisible();
    expect(
      await within(health_).findByText("Check unique_sale_id failed: 4 rows broke it."),
    ).toBeVisible();
  });

  it("says what a failed check found and that the table was set back", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [
              checkResult({ outcome: "failed", failing_rows: 12, rollback_outcome: "rolled_back" }),
            ],
          }),
        },
      },
    });

    expect(
      await within(await block()).findByText(
        "Check unique_sale_id failed: 12 rows broke it. The table was set back to its last good snapshot.",
      ),
    ).toBeVisible();
  });

  it("says a failed or warned check without a row count", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [
              checkResult({ name: "unique_sale_id", outcome: "failed", failing_rows: null }),
              checkResult({ name: "store_exists", outcome: "warned", failing_rows: null }),
            ],
          }),
        },
      },
    });

    const lines = await within(await block()).findAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual([
      "Check unique_sale_id failed.",
      "Check store_exists warned.",
    ]);
  });

  it("says an errored check could not run, with its error", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [checkResult({ outcome: "errored", error: "Column amount does not exist." })],
          }),
        },
      },
    });

    expect(
      await within(await block()).findByText(
        "Check unique_sale_id could not run: Column amount does not exist.",
      ),
    ).toBeVisible();
  });

  it("uses a signal's alert, else a neutral sentence for its kind", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [],
            signals: [
              signalResult({ state: "fired", signal: "volume" }),
              signalResult({
                id: "0c7d7f0e-5c10-4b64-a3f0-5a0b9f6b1002",
                state: "fired",
                signal: "freshness",
                alert: "No write for 2 days.",
              }),
            ],
          }),
        },
      },
    });

    const lines = await within(await block()).findAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual([
      "The number of rows written is outside its usual band.",
      "No write for 2 days.",
    ]);
  });

  it("never claims a direction when a signal's values are hidden", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [],
            signals: [
              signalResult({
                state: "fired",
                signal: "volume",
                alert: "Rows dropped by 40 %.",
                values_hidden: true,
              }),
            ],
          }),
        },
      },
    });

    const lines = await within(await block()).findAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual([
      "The number of rows written is outside its usual band. Its values are hidden for you.",
    ]);
    expect(within(await block()).queryByText(/rose|dropped/i)).not.toBeInTheDocument();
  });

  it("lists the fired lines by kind, blocking first, with a glyph and word each", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: [
              checkResult({ name: "w", outcome: "warned", failing_rows: 1 }),
              checkResult({ name: "f", outcome: "failed", failing_rows: 2 }),
            ],
            signals: [signalResult({ state: "fired", signal: "schema", effect: "block" })],
          }),
        },
      },
    });

    const lines = await within(await block()).findAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual([
      "The table's columns changed.",
      "Check f failed: 2 rows broke it.",
      "Check w warned: 1 row broke it.",
    ]);
    expect(lines[0]?.querySelector(".status-glyph-failed")).not.toBeNull();
    expect(lines[2]?.querySelector(".status-glyph-warning")).not.toBeNull();
  });

  it("shows at most five lines and counts the rest", async () => {
    renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: {
          status: 200,
          body: health({
            checks: Array.from({ length: 7 }, (_, index) =>
              checkResult({ name: `check_${String(index)}`, outcome: "failed", failing_rows: 1 }),
            ),
          }),
        },
      },
    });

    const health_ = await block();
    expect(await within(health_).findAllByRole("listitem")).toHaveLength(5);
    expect(within(health_).getByText("and 2 more")).toBeVisible();
  });

  it("says Healthy in a sentence", async () => {
    renderShell(PAGE);

    const health_ = await block();
    expect(
      await within(health_).findByText("Every check passed and no signal fired."),
    ).toBeVisible();
    expect(within(health_).getByText("Healthy")).toBeVisible();
    expect(within(health_).queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("does not claim every check passed when some are hidden from the reader", async () => {
    renderShell(PAGE, {
      routes: { [HEALTH_ROUTE]: { status: 200, body: health({ hidden_checks: 2 }) } },
    });

    const health_ = await block();
    expect(
      await within(health_).findByText("Every check you may see passed and no signal fired."),
    ).toBeVisible();
    expect(within(health_).queryByText("Every check passed and no signal fired.")).toBeNull();
  });

  it("calls a table without a verdict not judged", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ health_summary: null }) },
      },
    });

    expect(await within(await block()).findByText("Not judged")).toBeVisible();
  });

  it("says Not judged in a sentence", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({
            health_summary: {
              state: "not_judged",
              item: "not_judged",
              text: "Not judged",
              others: 0,
            },
          }),
        },
        [HEALTH_ROUTE]: { status: 200, body: health({ checks: [], signals: [] }) },
      },
    });

    expect(
      await within(await block()).findByText("Not judged yet: the table has no checks or signals."),
    ).toBeVisible();
  });

  it("names the cause and offers Try again when health cannot be read, with the rest of Overview in place", async () => {
    renderShell(PAGE, {
      routes: { [HEALTH_ROUTE]: { status: 500, body: { detail: "The health store broke." } } },
    });

    const health_ = await block();
    expect(await within(health_).findByRole("alert")).toHaveTextContent("The health store broke.");
    expect(within(health_).getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getByText("Every sale in every store, one row per sale.")).toBeVisible();
  });

  it("moves focus to the health heading after Try again succeeds", async () => {
    let calls = 0;
    const { user } = renderShell(PAGE, {
      routes: {
        [HEALTH_ROUTE]: () => {
          calls += 1;
          return calls === 1
            ? { status: 503, body: { detail: "Storage is down." } }
            : { status: 200, body: health({}) };
        },
      },
    });
    const health_ = await block();

    await user.click(await within(health_).findByRole("button", { name: "Try again" }));

    expect(
      await within(health_).findByText("Every check passed and no signal fired."),
    ).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "Health" })).toHaveFocus();
  });
});
