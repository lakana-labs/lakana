import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import type { LivePoll } from "../shell/useLivePoll";
import { isGone, PollProblem } from "./PollProblem";

/**
 * A poll in a given state.
 *
 * @param over - The fields that differ from a poll that read nothing yet.
 * @returns The poll.
 */
function aPoll(over: Partial<LivePoll<string>> = {}): LivePoll<string> {
  return {
    data: undefined,
    error: null,
    failure: null,
    reload: vi.fn(),
    busy: false,
    stale: false,
    failures: 0,
    ...over,
  };
}

/**
 * Renders the problem of a poll.
 *
 * @param poll - The poll.
 */
function renderProblem(poll: LivePoll<string>): void {
  render(
    <MemoryRouter>
      <PollProblem poll={poll} />
    </MemoryRouter>,
  );
}

const refused = (status: number): ApiError => new ApiError(status, "Refused", "Not for you.");

describe("PollProblem", () => {
  it("says nothing while all is well", () => {
    renderProblem(aPoll({ data: "x" }));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([404, 403])("says something gone (%s) with the way back to Jobs", (status) => {
    const poll = aPoll({ error: "Not for you.", failure: refused(status), failures: 1 });
    expect(isGone(poll)).toBe(true);
    renderProblem(poll);

    expect(screen.getByRole("alert")).toHaveTextContent("Not for you.");
    expect(screen.getByRole("link", { name: "Go to Jobs" })).toHaveAttribute("href", "/jobs");
  });

  it("offers Try again for a first read that failed otherwise", async () => {
    const poll = aPoll({ error: "The server is busy.", failure: refused(503), failures: 1 });
    expect(isGone(poll)).toBe(false);
    renderProblem(poll);

    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByRole("alert")).toHaveTextContent("The server is busy.");
    expect(poll.reload).toHaveBeenCalledTimes(1);
  });

  it("keeps quiet over older data until three reads in a row failed", () => {
    const { rerender } = render(
      <PollProblem poll={aPoll({ data: "x", error: "Lost.", stale: true, failures: 2 })} />,
    );
    expect(screen.queryByRole("alert")).toBeNull();

    rerender(<PollProblem poll={aPoll({ data: "x", error: "Lost.", stale: true, failures: 3 })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Lost.");
  });
});
