import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { aFailure, anAttempt } from "../test/jobs";
import { Attempts } from "./Attempts";

/**
 * The cells of a body row.
 *
 * @param index - The row's place among the body's rows.
 * @returns Its cells.
 */
function cells(index: number): HTMLElement[] {
  const body = screen.getAllByRole("rowgroup")[1] as HTMLElement;
  return within(within(body).getAllByRole("row")[index] as HTMLElement).getAllByRole("cell");
}

describe("Attempts", () => {
  it("lists attempts with right-aligned numbers and the failure code", () => {
    render(
      <Attempts
        asset={"silver.‮orders"}
        attempts={[
          anAttempt({
            outcome: "failed",
            worker: "w-‮1",
            failure: aFailure("memory_spike"),
            rows_written: null,
          }),
          anAttempt({ attempt: 2, outcome: "timed_out" }),
          anAttempt({ attempt: 3, outcome: "evicted" }),
        ]}
      />,
    );

    const region = screen.getByRole("region", { name: "Attempts of silver.U+202Eorders" });
    expect(region).toHaveAttribute("tabindex", "0");
    const headers = within(region)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers).toEqual([
      "Attempt",
      "Worker",
      "Started",
      "Duration",
      "Outcome",
      "Rows",
      "Failure",
      "Peak memory",
      "Processor time",
      "Bytes read",
    ]);
    const first = cells(0);
    expect(first.map((cell) => cell.textContent)).toEqual([
      "1",
      "w-U+202E1",
      expect.any(String),
      "30 s",
      "Failed",
      "–Not measured",
      "internal",
      "268.4 MB",
      "12 s",
      "52.4 MB",
    ]);
    for (const index of [0, 3, 5, 7, 8, 9]) {
      expect(first[index]).toHaveClass("number");
    }
    expect(cells(1)[4]).toHaveTextContent("Timed out");
    expect(cells(1)[5]).toHaveTextContent("1,200");
    expect(cells(2)[4]).toHaveTextContent("evicted");
  });

  it('an unmeasured figure shows a dash and says "Not measured", never zero', () => {
    render(
      <Attempts
        asset="silver.orders"
        attempts={[
          anAttempt({
            finished_at: null,
            outcome: null,
            rows_written: null,
            peak_memory_bytes: null,
            cpu_seconds: null,
            bytes_read: null,
          }),
        ]}
      />,
    );

    const row = cells(0);
    expect(row[3]).toHaveTextContent(/^$/u);
    expect(row[4]).toHaveTextContent("Running");
    // Rows is the sixth cell and Failure the seventh, which has no figure to miss.
    expect(row[6]).toHaveTextContent(/^$/u);
    for (const index of [5, 7, 8, 9]) {
      expect(row[index]).toHaveTextContent("–Not measured");
      expect(row[index]).not.toHaveTextContent("0");
    }
  });

  it("says when the task has no attempt yet", () => {
    render(<Attempts asset="silver.orders" attempts={[]} />);

    expect(screen.getByText("No attempt yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("lists two attempts that share a number, a released claim and its retry", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const attempts = [
      anAttempt({ outcome: "released", finished_at: "2026-11-17T01:00:06Z", rows_written: null }),
      anAttempt({ started_at: "2026-11-17T01:00:10Z", worker: "w-9b2c" }),
    ];
    const { rerender } = render(<Attempts asset="silver.orders" attempts={attempts} />);
    rerender(<Attempts asset="silver.orders" attempts={[...attempts].reverse()} />);

    expect(cells(0)[1]).toHaveTextContent("w-9b2c");
    expect(cells(1)[4]).toHaveTextContent("Released");
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});
