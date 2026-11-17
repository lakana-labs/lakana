import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { QueryNote } from "./QueryNote";

describe("QueryNote", () => {
  it("says what was deleted, marked, as a status that takes the focus", () => {
    render(<QueryNote note={{ kind: "deleted", name: "Daily \u202Esales" }} />);

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent("Deleted Daily U+202Esales.");
    expect(note).toHaveFocus();
  });
});
