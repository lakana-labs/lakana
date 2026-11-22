import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { QueryNote } from "./QueryNote";

describe("QueryNote", () => {
  it("says what was deleted, marked, and that a new query is open, as a status line", () => {
    render(<QueryNote note={{ kind: "deleted", name: "Daily ‮sales" }} />);

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent("Deleted Daily U+202Esales. A new, empty query is open.");
    expect(note).not.toHaveFocus();
  });

  it("says that a new query is open", () => {
    render(<QueryNote note={{ kind: "new" }} />);

    expect(screen.getByRole("status")).toHaveTextContent(/^A new, empty query is open\.$/);
  });

  it("stays mounted and empty without a note, so the next one is announced", () => {
    const { rerender } = render(<QueryNote note={null} />);
    const line = screen.getByRole("status");
    expect(line).toBeEmptyDOMElement();

    rerender(<QueryNote note={{ kind: "new" }} />);

    expect(screen.getByRole("status")).toBe(line);
    expect(line).toHaveTextContent("A new, empty query is open.");
  });
});
