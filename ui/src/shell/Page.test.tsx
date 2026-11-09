import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Page } from "./Page";

describe("Page", () => {
  it("renders a focusable heading, the content and the document title", () => {
    const { rerender } = render(
      <Page title="Jobs">
        <p>Content</p>
      </Page>,
    );

    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
    expect(screen.getByText("Content")).toBeInTheDocument();
    expect(document.title).toBe("Jobs · Lakana");

    rerender(
      <Page title="Catalog">
        <p>Content</p>
      </Page>,
    );
    expect(document.title).toBe("Catalog · Lakana");
  });

  it("puts the page's one primary action beside the heading", () => {
    render(
      <Page title="Access" action={<button type="button">Invite someone</button>}>
        <p>Content</p>
      </Page>,
    );

    const header = screen.getByRole("heading", { level: 1, name: "Access" }).parentElement;
    expect(header).toContainElement(screen.getByRole("button", { name: "Invite someone" }));
  });
});
