import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { ResultPageOut } from "../api";
import { result } from "../test/table";
import { pageCells, ResultGrid } from "./ResultGrid";

/** Every character the display rule marks, as a pattern; written with escapes only. */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
const RAW = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/u;

/**
 * Renders the grid of a page.
 *
 * @param page - The page.
 * @param label - The grid's label.
 * @returns The user.
 */
function show(
  page: ResultPageOut = result(),
  label = "fact_sales",
): ReturnType<typeof userEvent.setup> {
  const user = userEvent.setup();
  render(<ResultGrid page={page} cells={pageCells(page, i18next.t)} label={label} />);
  return user;
}

/**
 * The grid, once its rows are there.
 *
 * @param label - The grid's name.
 * @returns The grid.
 */
async function grid(label = "fact_sales"): Promise<HTMLElement> {
  const table = await screen.findByRole("grid", { name: `${label}, rows` });
  await waitFor(() => {
    expect(within(table).getAllByRole("row").length).toBeGreaterThan(0);
  });
  return table;
}

/**
 * The data cells of one row of the grid, without the row number's gutter.
 *
 * @param table - The grid.
 * @param index - The row, 1 for the first row of data.
 * @returns The cells' text.
 */
function rowText(table: HTMLElement, index: number): (string | null)[] {
  const row = within(table).getAllByRole("row")[index] as HTMLElement;
  // What a screen reader reads: text hidden from it is left out.
  return Array.from(row.querySelectorAll("td, th"), (cell) => cell)
    .filter((cell) => !cell.classList.contains("row-number"))
    .map((cell) => {
      const copy = cell.cloneNode(true) as Element;
      for (const hidden of copy.querySelectorAll("[aria-hidden='true']")) {
        hidden.remove();
      }
      return copy.textContent;
    });
}

describe("ResultGrid", () => {
  it("names the region and the grid from label with marks", () => {
    const page = result();
    show(page, "fact_‮sales");

    const region = screen.getByRole("region", { name: "fact_U+202Esales" });
    expect(within(region).getByRole("grid", { name: "fact_U+202Esales, rows" })).toBeVisible();
  });

  it("the strings come from grid.* and load with the module", () => {
    expect(i18next.t("grid.valueHint")).toBe(
      "Move to a cell with the arrow keys, or click one, to see its whole value here.",
    );
    expect(i18next.t("grid.cutMark")).toBe("… (cut)");
    expect(i18next.exists("table.preview.valueHint")).toBe(false);
  });

  it("cuts long values and shows one whole on focus or click", async () => {
    const long = "a".repeat(500);
    const user = show(result({ rows: [["1", "****", long, null, null]] }));
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

  it("draws the rows as the shared data grid with numbers aligned to the right", async () => {
    show();
    const table = await grid();

    expect(table).toHaveClass("data-grid");
    const headers = within(table).getAllByRole("columnheader");
    // The first header is the row numbers' gutter; sale_id (int64) is a number, the rest not.
    expect(headers.map((header) => header.classList.contains("number"))).toEqual([
      false,
      true,
      false,
      false,
      false,
      false,
    ]);
    const first = within(table).getAllByRole("row")[1] as HTMLElement;
    const cells = Array.from(first.querySelectorAll("td, th"));
    expect(cells[1]).toHaveClass("number");
    expect(cells[2]).not.toHaveClass("number");
  });

  it("numbers the rows in a gutter that is the row header and not a value", async () => {
    const user = show();
    const table = await grid();

    const gutter = within(table).getAllByRole("columnheader")[0] as HTMLElement;
    expect(gutter).toHaveClass("row-number");
    expect(gutter).toHaveTextContent("#");
    const rowHeaders = within(table).getAllByRole("rowheader");
    expect(rowHeaders.map((cell) => cell.textContent)).toEqual(["1", "2"]);
    expect(rowHeaders.every((cell) => cell.classList.contains("row-number"))).toBe(true);
    // No other column is a row header any more.
    expect(within(table).getAllByRole("rowheader")).toHaveLength(2);

    // A click on a row number shows no value; a click on a cell shows its own column.
    await user.click(rowHeaders[0] as HTMLElement);
    expect(screen.queryByRole("region", { name: /, row 1$/ })).not.toBeInTheDocument();
    await user.click(within(table).getAllByText("****")[0] as HTMLElement);
    expect(screen.getByRole("region", { name: "customer_email, row 1" })).toBeVisible();
  });

  it("is one tab stop and moves between cells with the arrow keys", async () => {
    const user = userEvent.setup();
    const page = result();
    render(
      <>
        <button type="button">Before</button>
        <ResultGrid page={page} cells={pageCells(page, i18next.t)} label="fact_sales" />
      </>,
    );
    const table = await grid();
    screen.getByRole("button", { name: "Before" }).focus();

    await user.tab();
    expect(table.contains(document.activeElement)).toBe(true);
    // The row, its number, then the cells.
    await user.keyboard("{ArrowRight}");
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
    show(
      result({
        columns: [{ name: "n‮ote", type: "string" }],
        rows: [["x‮y"]],
      }),
    );
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["xU+202Ey"]);
    expect(within(table).getAllByRole("columnheader")[1]).toHaveTextContent("nU+202Eote");
    expect(RAW.test(table.textContent)).toBe(false);
  });

  it("marks the cells the server left out of a cut row as cut", async () => {
    show(result({ rows: [["1", "****"]], row_cut: [0], columns_cut: 3 }));
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "… (cut)", "… (cut)", "… (cut)"]);
  });

  it("marks a struct cell the server cut", async () => {
    show(result({ rows: [["1", "****", "a", null, { cut: true }]] }));
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "a", "SQL null", "… (cut)"]);
  });

  it("marks cut values and rows", async () => {
    show(
      result({
        rows: [["1", "****", { cut: true, text: "abc" }, { cut: true }, { cut: true }]],
        row_cut: [0],
        columns_cut: 2,
      }),
    );
    const table = await grid();

    expect(rowText(table, 1)).toEqual(["1", "****", "abc… (cut)", "… (cut)", "… (cut)"]);
  });

  it("writes binary, null and struct cells as text", async () => {
    show();
    const table = await grid();

    expect(rowText(table, 1)).toEqual([
      "1",
      "****",
      "xU+202Ey",
      "binary, 12 bytes",
      '{"binary":3,"cut":true}',
    ]);
    expect(rowText(table, 2)).toEqual(["2", "****", "SQL null", "SQL null", "SQL null"]);
  });

  it("marks SQL NULL and binary as value markers, never like the text null", async () => {
    show(result({ rows: [["1", "null", null, { binary: 12 }, null]] }));
    const table = await grid();
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    const cells = Array.from(row.querySelectorAll("td, th")).slice(1);

    // The string "null" is text; SQL NULL and binary's size are markers, still read as words.
    expect(cells[1]?.querySelector(".value-marker")).toBeNull();
    expect(cells[1]).toHaveTextContent("null");
    expect(cells[2]?.querySelector(".value-marker [aria-hidden='true']")).toHaveTextContent(
      /^null$/,
    );
    // A screen reader tells SQL NULL from the text: the cells' names differ.
    expect(within(row).getAllByRole("gridcell", { name: "SQL null" })).toEqual([
      cells[2],
      cells[4],
    ]);
    expect(within(row).getByRole("gridcell", { name: "null" })).toBe(cells[1]);
    expect(cells[3]?.querySelector(".value-marker")).toHaveTextContent("binary, 12 bytes");
  });

  it("marks a hidden character of a column's name in the Value panel", async () => {
    const page = result();
    const user = show({
      ...page,
      columns: page.columns.map((column, index) =>
        index === 2 ? { ...column, name: "no⁦te" } : column,
      ),
    });
    const table = await grid();
    const rows = await within(table).findAllByRole("row");
    await user.click(rows[1]?.querySelectorAll("[role=gridcell]")[2] as HTMLElement);

    expect(await screen.findByRole("region", { name: "noU+2066te, row 1" })).toBeVisible();
  });
});
