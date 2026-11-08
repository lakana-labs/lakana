import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComingSoon } from "./ComingSoon";

const CLI = "lakana search <words>";

/**
 * Replaces `navigator.clipboard` for one test.
 *
 * @param value - The stand-in, or undefined for a browser without the Clipboard API.
 */
function setClipboard(value: unknown): void {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true, writable: true });
}

afterEach(() => {
  vi.restoreAllMocks();
  window.getSelection()?.removeAllRanges();
});

describe("ComingSoon", () => {
  it("names the part, the CLI line and one action", () => {
    render(<ComingSoon part={3} cli={CLI} />);

    expect(screen.getByText("This screen comes in part 3 of the interface.")).toBeInTheDocument();
    expect(screen.getByText("Until then, use the command line:")).toBeInTheDocument();
    expect(screen.getByText(CLI).tagName).toBe("CODE");
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
  });

  it("copies the command with the Clipboard API", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard({ writeText });
    render(<ComingSoon part={3} cli={CLI} />);

    await user.click(screen.getByRole("button", { name: "Copy command" }));

    expect(writeText).toHaveBeenCalledWith(CLI);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("selects the line and says so when the Clipboard API is missing", async () => {
    const user = userEvent.setup();
    setClipboard(undefined);
    render(<ComingSoon part={3} cli={CLI} />);

    await user.click(screen.getByRole("button", { name: "Copy command" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Select the line and copy it.");
    expect(window.getSelection()?.toString()).toBe(CLI);
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
  });

  it("selects the line when the browser refuses to write the clipboard", async () => {
    const user = userEvent.setup();
    setClipboard({
      writeText: () => Promise.reject(new DOMException("denied", "NotAllowedError")),
    });
    render(<ComingSoon part={4} cli='lakana sql "select 1"' />);

    await user.click(screen.getByRole("button", { name: "Copy command" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Select the line and copy it.");
    expect(window.getSelection()?.toString()).toBe('lakana sql "select 1"');
  });
});
