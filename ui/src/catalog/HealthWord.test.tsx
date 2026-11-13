import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HealthSummaryOut } from "../api";
import { HealthWord } from "./HealthWord";

function summary(overrides: Partial<HealthSummaryOut>): HealthSummaryOut {
  return { state: "healthy", item: "healthy", text: "Healthy", others: 0, ...overrides };
}

describe("HealthWord", () => {
  it("shows the worst word with its glyph and colour", () => {
    const { container } = render(
      <HealthWord
        summary={summary({ state: "failing", item: "check_failed", text: "Check failed" })}
      />,
    );

    expect(screen.getByText("Check failed")).toBeInTheDocument();
    expect(container.querySelector(".status-glyph-failed")).not.toBeNull();
  });

  it.each([
    ["warning", "late", "Late", ".status-glyph-warning"],
    ["healthy", "healthy", "Healthy", ".status-glyph-succeeded"],
    ["failing", "blocked_late", "Blocked: late", ".status-glyph-failed"],
  ] as const)("writes %s %s as %s", (state, item, word, glyph) => {
    const { container } = render(<HealthWord summary={summary({ state, item, text: "x" })} />);

    expect(screen.getByText(word)).toBeInTheDocument();
    expect(container.querySelector(glyph)).not.toBeNull();
  });

  it("counts the others visibly and in words", () => {
    render(
      <HealthWord summary={summary({ state: "warning", item: "late", text: "Late", others: 2 })} />,
    );

    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByText("Late, and 2 more, listed on the table page")).toHaveClass(
      "visually-hidden",
    );
  });

  it("adds nothing when there are no others", () => {
    render(<HealthWord summary={summary({ state: "warning", item: "late", others: 0 })} />);

    expect(screen.queryByText(/more, listed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
  });

  it("shows Not judged with a muted dash and no glyph", () => {
    const { container } = render(
      <HealthWord
        summary={summary({ state: "not_judged", item: "not_judged", text: "Not judged" })}
      />,
    );

    expect(container.querySelector("svg.status-glyph")).toBeNull();
    expect(screen.getByText("–")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Not judged")).toBeInTheDocument();
  });

  it("falls back to the state when the item is empty", () => {
    render(<HealthWord summary={summary({ state: "healthy", item: "", text: "" })} />);

    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("uses the server's word for an item it does not know", () => {
    render(
      <HealthWord
        summary={summary({ state: "warning", item: "new_signal", text: "New signal" })}
      />,
    );

    expect(screen.getByText("New signal")).toBeInTheDocument();
  });
});
