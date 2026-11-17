import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConnectionNote, listNote, pageNote } from "./ConnectionNote";

describe("ConnectionNote", () => {
  it.each([
    ["created", "Created crm. Test it now to see that a worker can reach it."],
    ["saved", "Saved crm."],
    ["deleted", "Deleted crm."],
  ] as const)("says %s as a status that takes the focus", (kind, words) => {
    render(<ConnectionNote note={{ kind, name: "crm" }} />);

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(words);
    expect(note).toHaveFocus();
  });

  it("marks a hidden character in the name", () => {
    render(<ConnectionNote note={{ kind: "deleted", name: "crm‮" }} />);

    expect(screen.getByRole("status")).toHaveTextContent("Deleted crmU+202E.");
  });

  it("reads the page's note from the address's state, whatever it holds", () => {
    expect(pageNote({ created: true }, "crm")).toEqual({ kind: "created", name: "crm" });
    expect(pageNote({ saved: true }, "crm")).toEqual({ kind: "saved", name: "crm" });
    expect(pageNote({ saved: "yes" }, "crm")).toBeNull();
    expect(pageNote(null, "crm")).toBeNull();
    expect(pageNote("created", "crm")).toBeNull();
  });

  it("reads the list's note from the address's state, whatever it holds", () => {
    expect(listNote({ note: { kind: "deleted", name: "crm" } })).toEqual({
      kind: "deleted",
      name: "crm",
    });
    expect(listNote({ note: { kind: "deleted", name: 3 } })).toBeNull();
    expect(listNote({ note: { kind: "created", name: "crm" } })).toBeNull();
    expect(listNote({ note: "deleted" })).toBeNull();
    expect(listNote(undefined)).toBeNull();
  });
});
