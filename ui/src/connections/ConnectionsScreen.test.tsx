import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { aConnection, connectionRoutes } from "../test/connections";
import { fakeServer, type FakeRoute } from "../test/server";
import { AreaScreen, FormLoading } from "./ConnectionsScreen";

/**
 * Renders the screen at an address below the area's path.
 *
 * @param path - The address.
 * @param routes - More routes.
 */
function renderAt(path: string, routes: Record<string, FakeRoute> = {}): void {
  fakeServer({
    ...connectionRoutes([aConnection()], [{ name: "prod", actions: ["test"] }]),
    "GET /api/v1/environments/prod/connections/crm": { status: 200, body: aConnection() },
    ...routes,
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <AreaScreen />
    </MemoryRouter>,
  );
}

describe("the Connections screen", () => {
  it("shows the list at its own address", async () => {
    renderAt("/");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Connections" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "crm" })).toBeInTheDocument();
  });

  it("shows the not-found state for any other address", () => {
    renderAt("/nowhere/at/all");

    expect(
      screen.getByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });

  it("shows Add at its own address, its heading there before the form's chunk loads", async () => {
    renderAt("/new");

    // At once, while the lazy form is still loading: the heading the shell focuses is mounted.
    const heading = screen.getByRole("heading", { level: 1, name: "Add connection" });
    expect(await screen.findByText(/You may not add connections/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Add connection" })).toBe(heading);
  });

  it("shows Edit of a connection at its own address", async () => {
    renderAt("/prod/crm/edit");

    const heading = screen.getByRole("heading", { level: 1, name: "Edit" });
    expect(await screen.findByRole("textbox", { name: "host" })).toHaveValue("db.example.org");
    // The same heading throughout, so the focus the shell gave it is kept.
    expect(screen.getByRole("heading", { level: 1, name: "Edit" })).toBe(heading);
  });

  it("shows the not-found state for Edit of a connection that does not exist", async () => {
    renderAt("/prod/gone/edit");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });

  it("offers Try again when Edit cannot read the connection", async () => {
    let reads = 0;
    renderAt("/prod/crm/edit", {
      "GET /api/v1/environments/prod/connections/crm": () => {
        reads += 1;
        return reads === 1
          ? { status: 500, body: { type: "InternalError", detail: "Down." } }
          : { status: 200, body: aConnection() };
      },
    });

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Edit" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("textbox", { name: "host" })).toBeInTheDocument();
  });

  it("says the form is loading once a second has passed", () => {
    vi.useFakeTimers();
    try {
      render(<FormLoading />);
      expect(screen.getByRole("status")).toHaveTextContent("");

      act(() => {
        vi.advanceTimersByTime(1_000);
      });

      expect(screen.getByRole("status")).toHaveTextContent("Loading the form…");
    } finally {
      vi.useRealTimers();
    }
  });
});
