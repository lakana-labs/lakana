import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { aJobRun } from "../test/jobs";
import { RunDuration } from "./RunDuration";

/**
 * Renders the duration of a run in a cell.
 *
 * @param over - The fields of the run that differ.
 * @returns The cell.
 */
function renderCell(over: Parameters<typeof aJobRun>[0]): HTMLElement {
  render(
    <table>
      <tbody>
        <tr>
          <td data-testid="cell">
            <RunDuration run={aJobRun(over)} />
          </td>
        </tr>
      </tbody>
    </table>,
  );
  return screen.getByTestId("cell");
}

describe("RunDuration", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says a queued run has not started yet, with a dash for the eye", () => {
    const cell = renderCell({ status: "queued", started_at: null, finished_at: null });

    expect(within(cell).getByText("–")).toHaveAttribute("aria-hidden", "true");
    expect(within(cell).getByText("Not started yet")).toHaveClass("visually-hidden");
  });

  it("shows a plain dash, never a time or a finish word, for a run cancelled before it started", () => {
    const cell = renderCell({
      status: "cancelled",
      started_at: null,
      finished_at: "2026-11-17T03:00:00Z",
    });

    expect(within(cell).getByText("–")).toHaveAttribute("aria-hidden", "true");
    expect(within(cell).getByText("Not measured")).toHaveClass("visually-hidden");
    expect(cell).not.toHaveTextContent("Not finished yet");
  });

  it("counts up to now while a run runs", () => {
    vi.useFakeTimers({ now: Date.parse("2026-11-17T03:00:30Z"), toFake: ["Date"] });

    expect(
      renderCell({ status: "running", started_at: "2026-11-17T03:00:00Z", finished_at: null }),
    ).toHaveTextContent("30 s");
  });

  it("counts to its end once a run ended", () => {
    expect(
      renderCell({ started_at: "2026-11-17T03:00:00Z", finished_at: "2026-11-17T03:02:00Z" }),
    ).toHaveTextContent("2 min");
  });
});
