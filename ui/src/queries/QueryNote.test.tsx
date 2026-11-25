import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { QueryNote } from "./QueryNote";

describe("QueryNote", () => {
  it("says what was deleted, marked, and that a new query is open, as a status line", async () => {
    render(<QueryNote note={{ kind: "deleted", name: "Daily \u202Esales", id: 1 }} />);

    const note = screen.getByRole("status");
    expect(await screen.findByText("Deleted Daily U+202Esales. A new, empty query is open.")).toBe(
      note,
    );
    expect(note).toHaveClass("queries-note");
    expect(note).not.toHaveClass("visually-hidden");
    expect(note).not.toHaveFocus();
  });

  it("says that a new query is open", async () => {
    render(<QueryNote note={{ kind: "new", id: 1 }} />);

    expect(await screen.findByText(/^A new, empty query is open\.$/)).toBe(
      screen.getByRole("status"),
    );
  });

  it("stays mounted, empty and visually hidden without a note, so the next one is announced", async () => {
    const { rerender } = render(<QueryNote note={null} />);
    const line = screen.getByRole("status");
    expect(line).toBeEmptyDOMElement();
    expect(line).toHaveClass("visually-hidden");

    rerender(<QueryNote note={{ kind: "new", id: 1 }} />);

    expect(await screen.findByText("A new, empty query is open.")).toBe(line);
    expect(line).not.toHaveClass("visually-hidden");
  });

  it("empties the line for a moment before a note that says the same, so it is announced again", async () => {
    const { rerender } = render(<QueryNote note={{ kind: "new", id: 1 }} />);
    const line = await screen.findByText("A new, empty query is open.");
    const seen: string[] = [];
    const watch = new MutationObserver(() => {
      seen.push(line.textContent);
    });
    watch.observe(line, { childList: true, characterData: true, subtree: true });

    rerender(<QueryNote note={{ kind: "new", id: 2 }} />);

    expect(line).toBeEmptyDOMElement();
    await screen.findByText("A new, empty query is open.");
    watch.disconnect();
    expect(seen).toEqual(["", "A new, empty query is open."]);
  });
});
