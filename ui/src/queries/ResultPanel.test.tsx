import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ResultPageOut } from "../api";
import type { QueryRunOut } from "../api/queries";
import { ResultGrid } from "../shell/ResultGrid";
import { aPage, aRun } from "../test/queries";
import { ResultPanel } from "./ResultPanel";

// The real grid, which one test replaces: a grid of 1,000 rows takes seconds to draw in jsdom.
vi.mock("../shell/ResultGrid", async (original) => {
  const real = await original<typeof import("../shell/ResultGrid")>();
  return { ...real, ResultGrid: vi.fn(real.ResultGrid) };
});

/**
 * A page of rows numbered from a first one.
 *
 * @param from - The first row's number.
 * @param rows - How many rows.
 * @param total - The rows of the whole result.
 * @returns The page, whose next offset follows it, or null at the end.
 */
function rowsFrom(from: number, rows: number, total: number): ResultPageOut {
  const end = from + rows;
  return aPage({
    rows: Array.from({ length: rows }, (_, at) => [from + at]),
    row_count: total,
    next_offset: end < total ? end : null,
  });
}

/**
 * Renders the panel for a run whose result pages grow by 100 rows on each Show 100 more.
 *
 * @param total - The rows of the whole result.
 * @param first - The rows of the first page.
 * @returns The user and the spy of `onMore`.
 */
function growing(total: number, first: number) {
  const onMore = vi.fn<() => Promise<void>>();
  /**
   * Holds the page the panel shows and appends to it.
   *
   * @returns The panel.
   */
  function Holder() {
    const [page, setPage] = useState(rowsFrom(0, first, total));
    onMore.mockImplementation(() => {
      setPage((shown) => {
        const next = rowsFrom(shown.rows.length, 100, total);
        return { ...next, rows: [...shown.rows, ...next.rows] };
      });
      return Promise.resolve();
    });
    return (
      <ResultPanel
        environment="prod"
        run={aRun()}
        page={page}
        name="Daily sales"
        saved={null}
        onMore={onMore}
      />
    );
  }
  const user = userEvent.setup();
  render(<Holder />);
  return { user, onMore };
}

/**
 * Renders the panel for a run and a page.
 *
 * @param page - The first page; null for a run without a stored result.
 * @param run - The run.
 * @param onMore - Reads the next page.
 */
function show(
  page: ResultPageOut | null,
  run: QueryRunOut = aRun(),
  onMore: () => Promise<void> = vi.fn(),
): void {
  render(
    <ResultPanel
      environment="prod"
      run={run}
      page={page}
      name="Daily sales"
      saved={null}
      onMore={onMore}
    />,
  );
}

describe("ResultPanel", () => {
  afterEach(() => {
    vi.mocked(ResultGrid).mockReset();
  });

  it("says rows and duration", () => {
    show(aPage({ rows: [[1], [2]], row_count: 1234 }));

    expect(screen.getByRole("status")).toHaveTextContent("1,234 rows · 1.0 s");
    expect(screen.getByRole("grid", { name: "Result of Daily sales, rows" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show 100 more" })).not.toBeInTheDocument();
  });

  it("puts the downloads after the line", () => {
    show(aPage());

    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download spreadsheet" })).toBeInTheDocument();
  });

  it("says one row in the singular and leaves out a duration it cannot know", () => {
    show(aPage(), aRun({ started_at: null }));

    expect(screen.getByRole("status")).toHaveTextContent(/^1 row$/);
  });

  it("says when the result has no rows", () => {
    show(aPage({ rows: [], row_count: 0 }));

    expect(screen.getByRole("status")).toHaveTextContent("0 rows · 1.0 s");
    expect(screen.getByText("The query returned no rows.")).toBeInTheDocument();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
  });

  it("says when the run's result is no longer kept", () => {
    show(null);

    expect(screen.getByText("This run's result is no longer kept.")).toBeInTheDocument();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
  });

  it("Show 100 more reads the next page and stops at 1,000 with the sentence", async () => {
    vi.mocked(ResultGrid).mockImplementation(({ page }) => (
      <p>{`${String(page.rows.length)} rows drawn`}</p>
    ));
    const { user, onMore } = growing(5_000, 100);
    expect(screen.getByText("100 rows drawn")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("5,000 rows · 1.0 s");

    for (let page = 1; page < 10; page += 1) {
      await user.click(screen.getByRole("button", { name: "Show 100 more" }));
    }

    expect(onMore).toHaveBeenCalledTimes(9);
    expect(screen.getByText("1000 rows drawn")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show 100 more" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Showing the first 1,000 of 5,000 rows; download the whole result.",
    );
  });

  it("shows the refusal of the next page next to the button, which stays usable", async () => {
    const user = userEvent.setup();
    const onMore = vi
      .fn<() => Promise<void>>()
      .mockRejectedValue(new ApiError(500, "InternalError", "Something broke."));
    show(rowsFrom(0, 100, 300), aRun(), onMore);

    await user.click(screen.getByRole("button", { name: "Show 100 more" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Something broke.");
    expect(screen.getByRole("button", { name: "Show 100 more" })).toBeEnabled();
  });

  it("names masked columns", () => {
    show(
      aPage({
        masked_columns: [
          { table: "gold.sales.fact_sales", column: "customer_email" },
          { table: "gold.orders", column: "email" },
        ],
        masked_columns_cut: 2,
      }),
    );

    const masked = screen.getByText("Masked for you:").parentElement as HTMLElement;
    expect(masked).toHaveTextContent(
      /^Masked for you: column customer_email of gold\.sales\.fact_sales, column email of gold\.orders and 2 more$/,
    );
  });

  it("says that values too large to send are marked cut", () => {
    show(aPage({ row_cut: [0] }));

    expect(
      screen.getByText("Some values were too large to send and are marked cut."),
    ).toBeInTheDocument();
  });
});
