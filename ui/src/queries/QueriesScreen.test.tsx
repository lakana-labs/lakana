import { EditorView } from "@codemirror/view";
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AreaScreen } from "./QueriesScreen";

describe("the Queries screen", () => {
  it("shows the editor named SQL, whose run and cancel keys do nothing yet", async () => {
    const { container } = render(<AreaScreen />);

    expect(screen.getByRole("heading", { level: 1, name: "Queries" })).toBeInTheDocument();
    const shadow = await waitFor(() => {
      const found = container.querySelector(".sql-editor")?.shadowRoot ?? null;
      expect(found).not.toBeNull();
      return found as ShadowRoot;
    });
    const box = within(shadow as unknown as HTMLElement).getByRole("textbox", { name: "SQL" });
    const view = EditorView.findFromDOM(box);
    view?.dispatch({ changes: { from: 0, insert: "select 1" } });

    const enter = new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, cancelable: true });
    box.dispatchEvent(enter);
    const escape = new KeyboardEvent("keydown", { key: "Escape", cancelable: true });
    box.dispatchEvent(escape);

    // Mod-Enter is taken (it inserts no line); Escape, with nothing to cancel, passes on.
    expect(enter.defaultPrevented).toBe(true);
    expect(escape.defaultPrevented).toBe(false);
    expect(view?.state.doc.toString()).toBe("select 1");
  });
});
