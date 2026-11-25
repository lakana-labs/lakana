import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { aJob } from "../test/jobs";
import { isWaiting, WaitingLabel } from "./JobWaiting";

const WAITS = { code: "job_waits", message: "A start waits for run 1b2c3d4e‮ to finish." };

describe("isWaiting", () => {
  it("is true for both waiting states and false for every other", () => {
    expect(isWaiting({ state: "waiting" })).toBe(true);
    expect(isWaiting({ state: "waiting to start" })).toBe(true);
    for (const state of ["scheduled", "paused", "no schedule", "no run principal"]) {
      expect(isWaiting({ state })).toBe(false);
    }
  });
});

describe("WaitingLabel", () => {
  it("is a quiet pill with the queued glyph and the word Waiting", () => {
    render(<WaitingLabel job={aJob({ state: "waiting", waiting_for: WAITS })} />);
    const word = screen.getByText("Waiting");
    expect(word.closest(".pill")).toHaveClass("pill-neutral");
    expect(document.querySelector("[data-tip]")).toBeNull();
  });

  it("in a list carries its sentence for the tooltip and a screen reader, marked", () => {
    render(<WaitingLabel tip job={aJob({ state: "waiting", waiting_for: WAITS })} />);
    const stop = document.querySelector<HTMLElement>("[data-tip]");
    expect(stop).toHaveAttribute("data-tip", "A start waits for run 1b2c3d4eU+202E to finish.");
    expect(stop).toHaveTextContent("Waiting A start waits for run 1b2c3d4e");
  });

  it("has no tooltip when the server sent no sentence", () => {
    render(<WaitingLabel tip job={aJob({ state: "waiting to start", waiting_for: null })} />);
    expect(screen.getByText("Waiting")).toBeInTheDocument();
    expect(document.querySelector("[data-tip]")).toBeNull();
  });
});
