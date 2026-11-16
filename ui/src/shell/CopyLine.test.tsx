import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyLine } from "./CopyLine";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CopyLine", () => {
  it("shows hidden characters as marks and copies the line exactly", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    const line = "lakana check list --env prod --table 'gold.fact_\u202Eselas'";
    const { container } = render(<CopyLine text={line} copyLabel="Copy command" />);

    const code = container.querySelector("code") as HTMLElement;
    expect(code).toHaveTextContent("gold.fact_U+202Eselas");
    expect(code.textContent).not.toContain("\u202E");
    expect(screen.getByText("U+202E")).toHaveClass("char-mark");

    await user.click(screen.getByRole("button", { name: "Copy command" }));

    expect(writeText).toHaveBeenCalledWith(line);
  });
});
