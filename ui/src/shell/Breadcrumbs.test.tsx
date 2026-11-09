import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "./Breadcrumbs";
import { Page } from "./Page";

/**
 * Renders a page with a trail inside a memory router, for its links.
 *
 * @param title - The page's title.
 */
function renderTrail(title: string): void {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <Page
            title={title}
            trail={[
              { to: "/access", label: "Access" },
              { to: "/access", label: "People" },
            ]}
          >
            <p>Content</p>
          </Page>
        ),
      },
    ],
    { initialEntries: ["/access/people/test1"] },
  );
  render(<RouterProvider router={router} />);
}

describe("Breadcrumbs", () => {
  it("names the landmark, links every part but the last and marks the last as the page", () => {
    renderTrail("test1");

    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    const items = within(nav).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["Access›", "People›", "test1"]);
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/access", "/access"]);
    const current = within(nav).getByText("test1");
    expect(current).toHaveAttribute("aria-current", "page");
    expect(current.tagName).not.toBe("A");
  });

  it("stands above the heading", () => {
    renderTrail("test1");

    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    const heading = screen.getByRole("heading", { level: 1, name: "test1" });
    expect(nav.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("hides the separators from screen readers", () => {
    render(
      <RouterProvider
        router={createMemoryRouter([
          {
            path: "*",
            element: <Breadcrumbs trail={[{ to: "/a", label: "A" }]} current="B" />,
          },
        ])}
      />,
    );

    const separator = screen.getByText("›");
    expect(separator).toHaveAttribute("aria-hidden", "true");
  });
});
