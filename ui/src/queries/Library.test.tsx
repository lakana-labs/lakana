import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { QUERY_ENVIRONMENTS, queryRoutes } from "../test/queries";
import { fakeServer } from "../test/server";
import { Library } from "./Library";

/**
 * The library with its filter kept as the screen keeps it.
 *
 * @param props - The filter it starts with.
 * @param props.start - The filter it starts with.
 * @returns The library.
 */
function Kept({ start }: { readonly start: string | null }): JSX.Element {
  const [filter, setFilter] = useState(start);
  return (
    <MemoryRouter>
      <Library
        me="kim.lee"
        environments={QUERY_ENVIRONMENTS}
        filter={filter}
        onFilter={setFilter}
        savedKey={0}
        recentKey={0}
      />
    </MemoryRouter>
  );
}

/**
 * The environment each list was last asked for.
 *
 * @param sent - Every request.
 * @returns The `environment` of the last read of each list, or null for none.
 */
function asked(sent: readonly { path: string }[]): Record<string, string | null> {
  const last: Record<string, string | null> = {};
  for (const request of sent) {
    const url = new URL(request.path, "http://localhost");
    last[url.pathname] = url.searchParams.get("environment");
  }
  return last;
}

describe("Library", () => {
  it("the filter applies to both lists and is remembered", async () => {
    const server = fakeServer(queryRoutes());
    const user = userEvent.setup();
    render(<Kept start={null} />);
    expect(screen.getByRole("heading", { level: 2, name: "Saved" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Recent" })).toBeInTheDocument();
    await screen.findByRole("region", { name: "Recent queries" });

    await user.click(screen.getByRole("button", { name: "Show: All environments" }));
    await user.click(screen.getByRole("option", { name: /^prod/ }));

    expect(screen.getByRole("button", { name: "Show: prod, protected" })).toBeInTheDocument();
    expect(window.localStorage.getItem("lakana.queries.filter")).toBe("prod");
    await waitFor(() => {
      expect(asked(server.sent)).toEqual({
        "/api/v1/saved-queries": "prod",
        "/api/v1/queries": "prod",
      });
    });

    await user.click(screen.getByRole("button", { name: "Show: prod, protected" }));
    await user.click(screen.getByRole("option", { name: "All environments" }));

    expect(window.localStorage.getItem("lakana.queries.filter")).toBeNull();
    await waitFor(() => {
      expect(asked(server.sent)).toEqual({
        "/api/v1/saved-queries": null,
        "/api/v1/queries": null,
      });
    });
  });

  it("lists all environments when the remembered filter is no longer listed", async () => {
    const server = fakeServer(queryRoutes());
    render(<Kept start="staging" />);

    expect(screen.getByRole("button", { name: "Show: All environments" })).toBeInTheDocument();
    await screen.findByRole("region", { name: "Recent queries" });
    expect(server.sent.filter((request) => request.path.includes("staging"))).toEqual([]);
  });
});
