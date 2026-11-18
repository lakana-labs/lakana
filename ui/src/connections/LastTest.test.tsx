import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { aConnection } from "../test/connections";
import { LastTest } from "./LastTest";
import { NO_WORKER_AFTER_MS } from "./useOpenTests";

const NOW = Date.parse("2026-11-17T12:00:00Z");
const QUEUED = {
  id: "t1",
  state: "queued",
  outcome: null,
  message: null,
  created_at: "2026-11-17T11:59:00Z",
  finished_at: null,
} as const;

describe("LastTest", () => {
  it("says queued, and where to look once no worker has taken the test for two minutes", () => {
    const { rerender } = render(
      <LastTest
        connection={aConnection({ last_test: QUEUED })}
        now={NOW}
        queuedSince={NOW - 1000}
      />,
    );

    expect(screen.getByText("queued")).toBeInTheDocument();
    expect(screen.queryByText(/No worker has taken this test yet/)).not.toBeInTheDocument();

    rerender(
      <LastTest
        connection={aConnection({ last_test: QUEUED })}
        now={NOW}
        queuedSince={NOW - NO_WORKER_AFTER_MS}
      />,
    );

    expect(screen.getByText("queued · no worker yet")).toBeInTheDocument();
    expect(
      screen.getByText(
        "No worker has taken this test yet. Check that a worker is running: docker compose ps worker, or the lakana local terminal.",
      ),
    ).toBeVisible();
  });

  it("names the outcome's cause and next step", () => {
    render(
      <LastTest
        connection={aConnection({
          updated_at: "2026-11-01T00:00:00Z",
          last_test: {
            state: "done",
            outcome: "tls",
            finished_at: "2026-11-17T11:00:00Z",
          },
        })}
        now={NOW}
      />,
    );

    expect(screen.getByText(/The TLS handshake failed/)).toBeInTheDocument();
    expect(screen.getByText(/Give the connection your CA certificate/)).toBeInTheDocument();
  });

  it("shows the test command only to people who may test the connection", () => {
    const { rerender } = render(<LastTest connection={aConnection({})} now={NOW} />);

    expect(screen.getByText(/lakana connection test/)).toBeInTheDocument();

    rerender(<LastTest connection={aConnection({ actions: [] })} now={NOW} />);

    expect(screen.queryByText(/lakana connection test/)).not.toBeInTheDocument();
  });
});
