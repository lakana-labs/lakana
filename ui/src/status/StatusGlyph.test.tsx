import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusGlyph } from "./StatusGlyph";
import { StatusLabel } from "./StatusLabel";
import { STATUS_KINDS } from "./status";

const WORDS = {
  succeeded: "Succeeded",
  failed: "Failed",
  warning: "Warning",
  running: "Running",
  queued: "Queued",
  "upstream-failed": "Upstream failed",
  cancelled: "Cancelled",
} as const;

describe("StatusGlyph", () => {
  it.each(STATUS_KINDS)("%s has its own silhouette", (kind) => {
    const shapes = STATUS_KINDS.map((other) => {
      const { container, unmount } = render(<StatusGlyph kind={other} />);
      const markup = container.querySelector("svg")?.innerHTML ?? "";
      unmount();
      return markup;
    });

    expect(shapes.filter((shape) => shape === shapes[STATUS_KINDS.indexOf(kind)])).toHaveLength(1);
    expect(shapes[STATUS_KINDS.indexOf(kind)]).not.toBe("");
  });

  it("is hidden from assistive technology when a word sits beside it", () => {
    const { container } = render(<StatusGlyph kind="failed" />);

    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("is announced with its label when it stands alone", () => {
    render(<StatusGlyph kind="failed" label="Failed" />);

    expect(screen.getByRole("img", { name: "Failed" })).toBeInTheDocument();
  });

  it.each(STATUS_KINDS)("StatusLabel shows the word for %s", (kind) => {
    render(<StatusLabel kind={kind} />);

    expect(screen.getByText(WORDS[kind])).toBeInTheDocument();
  });

  it("only the running glyph carries the pulse class", () => {
    for (const kind of STATUS_KINDS) {
      const { container, unmount } = render(<StatusGlyph kind={kind} />);
      const pulses = container.querySelector("svg")?.classList.contains("status-glyph-pulse");
      expect(pulses, kind).toBe(kind === "running");
      unmount();
    }
  });

  it("draws at 12 px with the same silhouette", () => {
    const big = render(<StatusGlyph kind="warning" />);
    const bigMarkup = big.container.querySelector("svg")?.innerHTML;
    expect(big.container.querySelector("svg")).toHaveAttribute("width", "16");
    big.unmount();

    const { container } = render(<StatusGlyph kind="warning" size={12} />);
    const svg = container.querySelector("svg");

    expect(svg).toHaveAttribute("width", "12");
    expect(svg).toHaveAttribute("height", "12");
    expect(svg).toHaveAttribute("viewBox", "0 0 16 16");
    expect(svg?.innerHTML).toBe(bigMarkup);
  });

  it("knock-out strokes use the knock-out token", () => {
    for (const kind of STATUS_KINDS) {
      const { container, unmount } = render(<StatusGlyph kind={kind} />);
      for (const element of container.querySelectorAll("[stroke], [fill]")) {
        for (const name of ["stroke", "fill"]) {
          const value = element.getAttribute(name);
          if (value !== null) {
            expect(["currentColor", "none", "var(--color-knockout)"], `${kind} ${name}`).toContain(
              value,
            );
          }
        }
      }
      unmount();
    }
  });
});
