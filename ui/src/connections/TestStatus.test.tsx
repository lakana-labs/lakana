import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { aConnection } from "../test/connections";
import { TestStatus } from "./TestStatus";

const NOW = Date.parse("2026-11-17T12:00:00Z");

describe("TestStatus", () => {
  it("draws the glyph, the word and the time in a time element", () => {
    const { container } = render(
      <TestStatus
        connection={aConnection({
          last_test: {
            state: "done",
            outcome: "credentials",
            finished_at: "2026-11-16T12:00:00Z",
          },
        })}
        now={NOW}
      />,
    );

    expect(container).toHaveTextContent("credentials · 1 day ago");
    expect(container.querySelector(".status-glyph-failed")).not.toBeNull();
    expect(screen.getByText("1 day ago").tagName).toBe("TIME");
    expect(screen.getByText("1 day ago")).toHaveAttribute("datetime", "2026-11-16T12:00:00Z");
  });

  it("dates a finished test without a finish time by the connection's last change", () => {
    render(
      <TestStatus
        connection={aConnection({
          updated_at: "2026-11-17T08:00:00Z",
          last_test: { state: "done", outcome: null, finished_at: null },
        })}
        now={NOW}
      />,
    );

    expect(screen.getByText("4 hours ago")).toHaveAttribute("datetime", "2026-11-17T08:00:00Z");
  });

  it("has no glyph and is muted for a test never run or one before the last change", () => {
    const { container, rerender } = render(
      <TestStatus connection={aConnection({ last_test: null })} now={NOW} />,
    );
    expect(container).toHaveTextContent("never tested");
    expect(container.querySelector(".status-glyph")).toBeNull();
    expect(container.firstElementChild).toHaveClass("test-status-muted");
    expect(container.querySelector("time")).toBeNull();

    rerender(
      <TestStatus
        connection={aConnection({
          updated_at: "2026-11-17T10:00:00Z",
          last_test: { state: "done", outcome: "ok", finished_at: "2026-11-14T12:00:00Z" },
        })}
        now={NOW}
      />,
    );
    expect(container).toHaveTextContent("tested before the last change · 3 days ago");
    expect(container.querySelector(".status-glyph")).toBeNull();
    expect(container.firstElementChild).toHaveClass("test-status-muted");
  });

  it("says no worker yet for a test queued for two minutes", () => {
    const { container } = render(
      <TestStatus
        connection={aConnection({
          last_test: { state: "queued", outcome: null, finished_at: null },
        })}
        now={NOW}
        queuedSince={NOW - 120_000}
      />,
    );

    expect(container).toHaveTextContent("queued · no worker yet");
    expect(container.querySelector(".status-glyph-queued")).not.toBeNull();
  });
});
