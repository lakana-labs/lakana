import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

const PEOPLE = [
  { name: "kim", type: "USER", is_active: true },
  { name: "loader", type: "SERVICE", is_active: true },
  { name: "old", type: "USER", is_active: false },
];

describe("PeoplePage", () => {
  it("lists people; every row opens the person", async () => {
    const { user, router } = renderShell("/access", {
      routes: {
        "GET /api/v1/principals": { status: 200, body: PEOPLE },
        "GET /api/v1/principals/kim/permissions": {
          status: 200,
          body: { permissions: [] },
        },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(within(row).getByRole("link")).toBeInTheDocument();
      expect(within(row).getByText("›")).toBeInTheDocument();
    }
    expect(within(rows[1] as HTMLElement).getByText("Service account")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Deactivated")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "People" })).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("link", { name: "kim" }));
    expect(router.state.location.pathname).toBe("/access/people/kim");
  });

  it("finds people by part of their name", async () => {
    const { user, server } = renderShell("/access", {
      routes: {
        "GET /api/v1/principals": (request) => ({
          status: 200,
          body: request.path.includes("q=ki") ? PEOPLE.slice(0, 1) : PEOPLE,
        }),
      },
    });
    await screen.findByRole("link", { name: "loader" });

    await user.type(screen.getByRole("searchbox", { name: "Find people" }), "ki");

    expect(await screen.findAllByRole("row")).toHaveLength(2);
    expect(server.sent.at(-1)?.path).toBe("/api/v1/principals?limit=200&q=ki");
  });

  it("says when nobody matches", async () => {
    renderShell("/access", {
      routes: { "GET /api/v1/principals": { status: 200, body: [] } },
    });

    expect(await screen.findByText("Nobody matches.")).toBeInTheDocument();
  });
});
