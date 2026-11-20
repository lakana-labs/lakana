import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { aConnection } from "../test/connections";
import { LastTest } from "./LastTest";
import { RunFailure } from "./RunFailure";

const NOW = Date.parse("2026-11-19T12:00:00Z");
const FAILURE = {
  run_id: "0b6f1c1e-5b0e-4a49-9c1e-6a2f1d3c4b5a",
  asset: "silver.customers",
  finished_at: "2026-11-19T10:00:00Z",
} as const;
const TASK = "/jobs/prod/runs/0b6f1c1e-5b0e-4a49-9c1e-6a2f1d3c4b5a/tasks/silver.customers";

describe("RunFailure", () => {
  it("links the words to the task in its run, with the failed glyph and the time ago", () => {
    render(
      <MemoryRouter>
        <RunFailure environment="prod" failure={FAILURE} now={NOW} />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "last run failed on it · 2 hours ago" });
    expect(link).toHaveAttribute("href", TASK);
    expect(link.querySelector("time")).toHaveAttribute("datetime", FAILURE.finished_at);
    expect(link.parentElement?.querySelector(".status-glyph-failed")).toBeInTheDocument();
    expect(screen.queryByText(/failed reading it/)).not.toBeInTheDocument();
  });

  it("says just now within a minute of the failure", () => {
    render(
      <MemoryRouter>
        <RunFailure
          environment="prod"
          failure={{ ...FAILURE, finished_at: "2026-11-19T11:59:40Z" }}
          now={NOW}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link")).toHaveTextContent("last run failed on it · just now");
  });

  it("writes the full date and names the task on the connection's page", () => {
    render(
      <MemoryRouter>
        <LastTest connection={aConnection({ last_run_failure: FAILURE })} now={NOW} />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: /^last run failed on it · / });
    expect(link).toHaveAttribute("href", TASK);
    expect(link).not.toHaveTextContent("ago");
    expect(
      screen.getByText("silver.customers failed reading it; open the task to read why."),
    ).toBeVisible();
  });
});
