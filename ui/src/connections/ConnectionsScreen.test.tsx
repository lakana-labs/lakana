import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { aConnection, connectionRoutes } from "../test/connections";
import { fakeServer } from "../test/server";
import { AreaScreen, FormLoading } from "./ConnectionsScreen";

/**
 * Renders the screen at an address below the area's path.
 *
 * @param path - The address.
 */
function renderAt(path: string): void {
  fakeServer(connectionRoutes([aConnection()], [{ name: "prod", actions: ["test"] }]));
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

  it("shows Add at its own address", async () => {
    renderAt("/new");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Add connection" }),
    ).toBeInTheDocument();
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
