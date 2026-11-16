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

  it("marks hidden characters in the heading and the document title", () => {
    render(
      <Page title={"fact_\u202Eselas"}>
        <p>Content</p>
      </Page>,
    );

    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("fact_U+202Eselas");
    expect(heading.textContent).not.toContain("\u202E");
    expect(screen.getByText("U+202E")).toHaveClass("char-mark");
    expect(document.title).toBe("fact_U+202Eselas · Lakana");
  });
});
