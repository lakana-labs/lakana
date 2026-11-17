import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { queryRoutes } from "../test/queries";
import { renderShell } from "../test/render";

describe("the Queries screen", () => {
  it("shows the heading and the editor named SQL, whose Escape passes on with nothing to cancel", async () => {
    renderShell("/queries", { routes: queryRoutes() });

    expect(await screen.findByRole("heading", { level: 1, name: "Queries" })).toBeInTheDocument();
    const shadow = await waitFor(() => {
      const found = document.querySelector(".sql-editor")?.shadowRoot ?? null;
      expect(found).not.toBeNull();
      return found as ShadowRoot;
    });
    const box = within(shadow as unknown as HTMLElement).getByRole("textbox", { name: "SQL" });
    const escape = new KeyboardEvent("keydown", { key: "Escape", cancelable: true });
    box.dispatchEvent(escape);

    expect(escape.defaultPrevented).toBe(false);
    expect(screen.getByText("Untitled query")).toBeInTheDocument();
  });
});
