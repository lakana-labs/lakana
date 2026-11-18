import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { aHeldGrant } from "../test/access";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/access/people/maria";

/**
 * The routes of maria's page.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function personRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    "GET /api/v1/principals/maria": {
      status: 200,
      body: { name: "maria", type: "USER", is_active: true, picture: null, held_admin: false },
    },
    "GET /api/v1/principals/maria/passkeys": { status: 200, body: [] },
    "GET /api/v1/principals/maria/sessions": { status: 200, body: [] },
    "GET /api/v1/principals/maria/grants": {
      status: 200,
      body: [aHeldGrant(), aHeldGrant({ id: "h-2", role: "VIEWER", group: "analysts" })],
    },
    "GET /api/v1/groups": { status: 200, body: [{ name: "analysts" }, { name: "ops" }] },
    ...more,
  };
}

describe("PersonGrants", () => {
  it("shows the person's grants and groups", async () => {
    const { server } = renderShell(PATH, { routes: personRoutes() });

    const region = await screen.findByRole("region", { name: "Grants of maria" });
    const [, direct, through] = within(region).getAllByRole("row");
    expect(direct).toHaveTextContent("directly");
    expect(
      within(through as HTMLElement).getByRole("link", { name: "analysts" }),
    ).toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Grants and groups" });
    const groups = within(panel).getAllByRole("listitem");
    expect(groups.map((item) => item.textContent)).toEqual(["analysts", "ops"]);
    expect(within(groups[1] as HTMLElement).getByRole("link")).toHaveAttribute(
      "href",
      "/settings/access/groups/ops",
    );
    expect(within(panel).getByRole("link", { name: "Manage grants" })).toHaveAttribute(
      "href",
      "/settings/access/grants?principal=maria",
    );
    expect(
      server.sent.some((request) => request.path === "/api/v1/groups?member=maria&limit=50"),
    ).toBe(true);
  });

  it("says when the person holds nothing", async () => {
    renderShell(PATH, {
      routes: personRoutes({
        "GET /api/v1/principals/maria/grants": { status: 200, body: [] },
        "GET /api/v1/groups": { status: 200, body: [] },
      }),
    });

    expect(await screen.findByText("maria holds no grant and is in no group.")).toBeInTheDocument();
  });

  it("says when the person is in no group", async () => {
    renderShell(PATH, {
      routes: personRoutes({ "GET /api/v1/groups": { status: 200, body: [] } }),
    });

    expect(await screen.findByText("In no group.")).toBeInTheDocument();
  });

  it("reads again after a failure, keeping the focus on the panel", async () => {
    let tries = 0;
    const { user } = renderShell(PATH, {
      routes: personRoutes({
        "GET /api/v1/principals/maria/grants": () => {
          tries += 1;
          return tries === 1
            ? { status: 500, body: { detail: "The grants are not there." } }
            : { status: 200, body: [] };
        },
        "GET /api/v1/groups": () =>
          tries <= 1
            ? { status: 500, body: { detail: "The groups are not there." } }
            : { status: 200, body: [] },
      }),
    });

    const panel = await screen.findByRole("region", { name: "Grants and groups" });
    const buttons = await within(panel).findAllByRole("button", { name: "Try again" });
    expect(buttons).toHaveLength(2);
    await user.click(buttons[0] as HTMLElement);
    await user.click(within(panel).getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("maria holds no grant and is in no group.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Grants and groups" })).toHaveFocus();
  });
});
