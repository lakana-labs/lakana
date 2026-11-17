import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import { aPage, aRun } from "../test/queries";
import { QueryError } from "./QueryError";
import type { QueryRunState } from "./useQueryRun";

const IDLE: QueryRunState = { phase: "idle", run: null, page: null, error: null };

/**
 * Renders the error for a state.
 *
 * @param state - The run's state.
 * @returns The callbacks' spies and the user.
 */
function show(state: QueryRunState) {
  const onGoTo = vi.fn();
  const onRetry = vi.fn();
  const user = userEvent.setup();
  const { container } = render(<QueryError state={state} onGoTo={onGoTo} onRetry={onRetry} />);
  return { onGoTo, onRetry, user, container };
}

describe("QueryError", () => {
  it("shows a refusal of the submit in the server's words", () => {
    show({
      ...IDLE,
      phase: "refused",
      error: new ApiError(422, "QueryRefused", "Only one statement may run."),
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Only one statement may run.");
  });

  it("shows a failed run's message with its hidden characters marked, and Go to", async () => {
    const message = `Column ${String.fromCodePoint(0x202e)}x does not exist.`;
    const { onGoTo, user } = show({
      ...IDLE,
      phase: "done",
      run: aRun({ status: "FAILED", error_message: message, error_line: 2, error_column: 8 }),
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Column U+202Ex does not exist.");
    await user.click(screen.getByRole("button", { name: "Go to line 2, column 8" }));
    expect(onGoTo).toHaveBeenCalledWith(2, 8);
  });

  it("words a failed or cancelled run without a message, and offers no Go to without a place", () => {
    const { container } = show({ ...IDLE, phase: "done", run: aRun({ status: "FAILED" }) });
    expect(screen.getByRole("alert")).toHaveTextContent("The query failed.");
    container.remove();

    show({ ...IDLE, phase: "done", run: aRun({ status: "CANCELLED", error_line: 3 }) });
    expect(screen.getByRole("alert")).toHaveTextContent("The query was cancelled.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("says the state could not be read, and Try again follows the run again", async () => {
    const { onRetry, user } = show({
      ...IDLE,
      phase: "failed",
      error: new ApiError(503, "Busy", "The server is busy."),
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The state of the query could not be read: The server is busy.",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("shows nothing while there is nothing wrong", () => {
    const { container } = show({ ...IDLE, phase: "done", run: aRun(), page: aPage() });

    expect(container).toBeEmptyDOMElement();
  });
});
