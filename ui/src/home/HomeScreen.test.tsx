import { cleanup, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DONE, STATE } from "../test/home";
import { renderShell } from "../test/render";

const OPEN = {
  audience: "reader",
  local: false,
  steps: [
    { id: "open_table", done: true },
    { id: "run_query", done: false },
  ],
  hidden: false,
  complete: false,
};

/**
 * The names of Home's sections, top to bottom.
 *
 * @returns The second-level headings' texts.
 */
function sections(): string[] {
  return screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
}

describe("HomeScreen", () => {
  it("puts Get started above the status box until it is complete or hidden", async () => {
    renderShell("/", { routes: { "GET /api/v1/home/get-started": { status: 200, body: OPEN } } });

    expect(await screen.findByRole("heading", { level: 2, name: "Get started" })).toBeVisible();
    await screen.findByText("Healthy");
    expect(sections()).toEqual([
      "Get started",
      "Status",
      "Needs your attention",
      "Recently opened",
      "Your work",
    ]);
    cleanup();

    renderShell("/", { routes: { "GET /api/v1/home/get-started": { status: 200, body: DONE } } });
    await screen.findByText("Healthy");
    expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument();
  });

  it("keeps hidden Get started hidden after a reload", async () => {
    let state = STATE;
    const routes = {
      "GET /api/v1/home/get-started": { status: 200, body: OPEN },
      "GET /api/v1/principals/admin/interface-state": () => ({ status: 200, body: state }),
      "PATCH /api/v1/principals/admin/interface-state": () => {
        state = { ...state, get_started_hidden: true };
        return { status: 200, body: state };
      },
    };
    const { user, server } = renderShell("/", { routes });

    const list = await screen.findByRole("region", { name: "Get started" });
    await user.click(within(list).getByRole("button", { name: "Hide" }));

    expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({
      get_started_hidden: true,
    });
    expect(screen.queryByRole("region", { name: "Get started" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toHaveFocus();
    cleanup();

    renderShell("/", {
      routes: {
        ...routes,
        "GET /api/v1/home/get-started": { status: 200, body: { ...OPEN, hidden: true } },
      },
    });
    await screen.findByText("Healthy");
    expect(screen.queryByRole("region", { name: "Get started" })).not.toBeInTheDocument();
  });

  it("follows the list's own flag when the interface state cannot be read", async () => {
    renderShell("/", {
      routes: {
        "GET /api/v1/home/get-started": { status: 200, body: OPEN },
        "GET /api/v1/principals/admin/interface-state": {
          status: 503,
          body: { detail: "Busy.", type: "Busy" },
        },
      },
    });

    expect(await screen.findByRole("region", { name: "Get started" })).toBeVisible();
  });

  it("says why when hiding Get started did not work", async () => {
    const { user } = renderShell("/", {
      routes: {
        "GET /api/v1/home/get-started": { status: 200, body: OPEN },
        "PATCH /api/v1/principals/admin/interface-state": {
          status: 503,
          body: { detail: "The database is busy. Try again in a minute.", type: "Busy" },
        },
      },
    });

    const list = await screen.findByRole("region", { name: "Get started" });
    await user.click(within(list).getByRole("button", { name: "Hide" }));

    expect(await within(list).findByRole("alert")).toHaveTextContent("The database is busy.");
    expect(screen.getByRole("region", { name: "Get started" })).toBeInTheDocument();
  });

  it("says why Get started could not be read, and shows the rest", async () => {
    renderShell("/", {
      routes: {
        "GET /api/v1/home/get-started": {
          status: 503,
          body: { detail: "The database is busy. Try again in a minute.", type: "Busy" },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The database is busy.");
    expect(await screen.findByText("Healthy")).toBeVisible();
  });

  it("shows only the no-access line to a person without access", async () => {
    const { server } = renderShell("/", {
      access: new Set(["home", "catalog", "queries", "jobs", "connections"]),
      me: { name: "kim", admin: false, mayDeploy: false, noAccessYet: true },
    });

    expect(
      await screen.findByText("You have no access yet; ask an administrator."),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("heading", { level: 2 })).toEqual([]);
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/home"))).toBe(false);
    expect(server.sent.some((request) => request.path === "/api/v1/status")).toBe(false);
  });

  it("shows Your work only to people who may deploy", async () => {
    const { server } = renderShell("/", {
      access: new Set(["home", "catalog", "queries", "jobs", "connections"]),
      me: { name: "kim", admin: false, mayDeploy: false },
    });

    await screen.findByText("Healthy");
    expect(sections()).toEqual(["Status", "Needs your attention", "Recently opened"]);
    expect(server.sent.some((request) => request.path === "/api/v1/home/work")).toBe(false);
  });
});
