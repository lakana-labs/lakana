import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { ConnectionEnvironmentOut } from "../api";
import { aConnection, connectionRoutes, ENVIRONMENTS_ROUTE } from "../test/connections";
import { held } from "../test/pending";
import { fakeServer, type FakeRoute } from "../test/server";
import { MeContext } from "../shell/me";
import { ConnectionList } from "./ConnectionList";

const LIST = "GET /api/v1/connections";
const MANAGER: ConnectionEnvironmentOut[] = [
  { name: "dev", actions: ["create", "test", "name_secret"] },
  { name: "prod", actions: ["create", "test", "name_secret"] },
];
const ME = { name: "kim.lee", admin: false, noAccessYet: false, mayDeploy: false, picture: null };

/**
 * Renders the list as `kim.lee`.
 *
 * @returns What `render` hands back.
 */
function renderList(): ReturnType<typeof render> {
  return render(
    <MeContext value={ME}>
      <MemoryRouter>
        <ConnectionList />
      </MemoryRouter>
    </MeContext>,
  );
}

/**
 * The list's rows by cursor.
 *
 * @param pages - The pages by cursor; "" for the first.
 * @returns The route.
 */
function byCursor(
  pages: Record<string, { items: unknown[]; next_cursor: string | null }>,
): FakeRoute {
  return (request) => {
    const cursor = new URL(request.path, "http://localhost").searchParams.get("cursor") ?? "";
    return { status: 200, body: pages[cursor] };
  };
}

describe("ConnectionList", () => {
  it("lists every readable environment's connections with kind, where, environment, status and used by", async () => {
    fakeServer(
      connectionRoutes(
        [
          aConnection({ declared_by: [{ repository: "shop", asset: "orders" }] }),
          aConnection({
            name: "files",
            kind: "directory",
            address: { root: "incoming", path: "daily" },
            environment: "dev",
            secret: null,
            declared_by: [
              { repository: "shop", asset: "a" },
              { repository: "shop", asset: "b" },
            ],
            last_test: null,
          }),
        ],
        MANAGER,
      ),
    );
    renderList();

    const rows = await screen.findAllByRole("row");
    const crm = within(rows[1] as HTMLElement).getAllByRole("cell");
    expect(crm.map((cell) => cell.textContent)).toEqual([
      expect.stringContaining("crm") as string,
      "PostgreSQL",
      "db.example.org:5432/crm",
      "Environment prod",
      expect.stringMatching(/^OK · .* ago$/) as string,
      "1 asset",
      "Test",
      "›",
    ]);
    const files = within(rows[2] as HTMLElement).getAllByRole("cell");
    expect(files.map((cell) => cell.textContent)).toEqual([
      expect.stringContaining("files") as string,
      "Directory",
      "incoming:daily",
      "Environment dev",
      "never tested",
      "2 assets",
      "Test",
      "›",
    ]);
    expect(
      within(rows[2] as HTMLElement)
        .getByText("never tested")
        .closest(".test-status"),
    ).toHaveClass("test-status-muted");
  });

  it("the row's name opens the connection's page", async () => {
    fakeServer(connectionRoutes([aConnection({ name: "a b" })], MANAGER));
    renderList();

    expect(await screen.findByRole("link", { name: "a b" })).toHaveAttribute(
      "href",
      "/connections/prod/a%20b",
    );
  });

  it("offers Test only on rows whose actions include test, and Add only when an environment allows create", async () => {
    fakeServer(
      connectionRoutes(
        [aConnection(), aConnection({ name: "other", actions: [] })],
        [{ name: "prod", actions: [] }],
      ),
    );
    renderList();

    await screen.findByRole("link", { name: "crm" });
    expect(screen.getAllByRole("button", { name: /^Test / })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Test crm" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add connection" })).not.toBeInTheDocument();
  });

  it("offers Add to the new connection's page when an environment allows create", async () => {
    fakeServer(connectionRoutes([aConnection()], MANAGER));
    renderList();

    expect(await screen.findByRole("link", { name: "Add connection" })).toHaveAttribute(
      "href",
      "/connections/new",
    );
  });

  it("remembers the environment filter and asks the list with it", async () => {
    const server = fakeServer(
      connectionRoutes(
        [aConnection(), aConnection({ name: "files", environment: "dev" })],
        MANAGER,
      ),
    );
    const user = userEvent.setup();
    renderList();
    await screen.findByRole("link", { name: "files" });

    await user.click(screen.getByRole("button", { name: "Environment: All environments" }));
    await user.click(screen.getByRole("option", { name: "dev" }));

    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "crm" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "files" })).toBeInTheDocument();
    expect(server.sent.at(-1)?.path).toBe("/api/v1/connections?limit=100&environment=dev");
    expect(window.localStorage.getItem("lakana.connections.kim.lee")).toBe("dev");

    await user.click(screen.getByRole("button", { name: "Environment: dev" }));
    await user.click(screen.getByRole("option", { name: "All environments" }));
    await screen.findByRole("link", { name: "crm" });
    expect(window.localStorage.getItem("lakana.connections.kim.lee")).toBeNull();
  });

  it("starts from the remembered environment", async () => {
    window.localStorage.setItem("lakana.connections.kim.lee", "dev");
    const server = fakeServer(
      connectionRoutes([aConnection({ name: "files", environment: "dev" })], MANAGER),
    );
    renderList();

    await screen.findByRole("link", { name: "files" });
    expect(screen.getByRole("button", { name: "Environment: dev" })).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.startsWith(LIST.slice(4)))).toHaveLength(2);
    expect(server.sent.some((request) => request.path.includes("environment=dev"))).toBe(true);
  });

  it("falls back to all environments when the remembered one is gone", async () => {
    window.localStorage.setItem("lakana.connections.kim.lee", "staging");
    const server = fakeServer(connectionRoutes([aConnection()], MANAGER));
    renderList();

    await screen.findByRole("link", { name: "crm" });
    expect(
      screen.getByRole("button", { name: "Environment: All environments" }),
    ).toBeInTheDocument();
    expect(server.sent.some((request) => request.path.includes("staging"))).toBe(false);
  });

  it("shows more by cursor", async () => {
    const user = userEvent.setup();
    fakeServer({
      [ENVIRONMENTS_ROUTE]: { status: 200, body: MANAGER },
      [LIST]: byCursor({
        "": { items: [aConnection({ name: "one" })], next_cursor: "c2" },
        c2: { items: [aConnection({ name: "two" })], next_cursor: null },
      }),
    });
    renderList();
    await screen.findByRole("link", { name: "one" });

    await user.click(screen.getByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("link", { name: "two" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("says when there is none, and how to add one from the command line", async () => {
    window.localStorage.setItem("lakana.connections.kim.lee", "dev");
    fakeServer(connectionRoutes([], MANAGER));
    const { container, unmount } = renderList();

    expect(await screen.findByText("No connections in dev yet.")).toBeInTheDocument();
    expect(container).toHaveTextContent(
      "lakana connection create NAME --env 'dev' --kind directory --root ROOT --path PATH",
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    unmount();

    window.localStorage.clear();
    renderList();
    expect(await screen.findByText("No connections yet.")).toBeInTheDocument();
    expect(document.body).toHaveTextContent("--env 'dev'");
  });

  it("says how to get the right when no environment is readable", async () => {
    const server = fakeServer(connectionRoutes([], []));
    const { container } = renderList();

    expect(
      await screen.findByText(
        "You may not read the connections of any environment yet. Ask an administrator for READ_ENVIRONMENT on one.",
      ),
    ).toBeInTheDocument();
    expect(container).toHaveTextContent("lakana connection list --env <environment>");
    expect(screen.queryByRole("link", { name: "Add connection" })).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/connections?"))).toBe(
      false,
    );
  });

  it("the server's refusal shows through problemText with Try again", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      [ENVIRONMENTS_ROUTE]: {
        status: 503,
        body: { detail: "The server is busy; try again.", type: "Busy" },
      },
    });
    renderList();

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy; try again.");
    server.route(connectionRoutes([aConnection()], MANAGER));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "crm" })).toBeInTheDocument();
  });

  it("shows the refusal of the connections themselves with Try again", async () => {
    fakeServer({
      [ENVIRONMENTS_ROUTE]: { status: 200, body: MANAGER },
      [LIST]: { status: 503, body: { detail: "The list is busy; try again.", type: "Busy" } },
    });
    renderList();

    expect(await screen.findByRole("alert")).toHaveTextContent("The list is busy; try again.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("marks hidden characters in names and values", async () => {
    fakeServer(
      connectionRoutes(
        [
          aConnection({
            name: "cr‮m",
            address: { host: "db‮.example.org", port: 5432, database: "crm" },
          }),
        ],
        MANAGER,
      ),
    );
    renderList();

    expect(await screen.findByRole("link", { name: "crU+202Em" })).toBeInTheDocument();
    expect(screen.getAllByText(/dbU\+202E\.example\.org/).length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("‮");
  });

  describe("Test", () => {
    it("asks for a test and shows it open, with Test disabled and in words", async () => {
      const answer = held();
      const server = fakeServer({
        ...connectionRoutes([aConnection()], MANAGER),
        "POST /api/v1/environments/prod/connections/crm/tests": answer.route,
      });
      const user = userEvent.setup();
      renderList();

      await user.click(await screen.findByRole("button", { name: "Test crm" }));
      answer.release({
        status: 202,
        body: {
          id: "t1",
          state: "queued",
          outcome: null,
          message: null,
          created_at: "2026-11-17T10:00:00Z",
          finished_at: null,
        },
      });

      const row = (await screen.findAllByRole("row"))[1] as HTMLElement;
      await waitFor(() => {
        expect(within(row).getByText("queued")).toBeInTheDocument();
      });
      expect(within(row).getByRole("button", { name: "Test crm" })).toBeDisabled();
      expect(within(row).getByRole("button", { name: "Test crm" })).toHaveTextContent("Testing…");
      expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
    });

    it("is disabled on load for a test that is already open, and sends nothing", async () => {
      const server = fakeServer(
        connectionRoutes(
          [aConnection({ last_test: { state: "running", outcome: null, finished_at: null } })],
          MANAGER,
        ),
      );
      renderList();

      expect(await screen.findByRole("button", { name: "Test crm" })).toBeDisabled();
      expect(screen.getByText("testing…")).toBeInTheDocument();
      expect(server.sent.some((request) => request.method === "POST")).toBe(false);
    });

    it("shows a refusal in the row's alert and offers Test again", async () => {
      let refuse = true;
      fakeServer({
        ...connectionRoutes([aConnection()], MANAGER),
        "POST /api/v1/environments/prod/connections/crm/tests": () =>
          refuse
            ? {
                status: 403,
                body: { detail: "You may not test connections in prod.", type: "Forbidden" },
              }
            : {
                status: 202,
                body: {
                  id: "t",
                  state: "queued",
                  outcome: null,
                  message: null,
                  created_at: "2026-11-17T10:00:00Z",
                  finished_at: null,
                },
              },
      });
      const user = userEvent.setup();
      renderList();

      await user.click(await screen.findByRole("button", { name: "Test crm" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "You may not test connections in prod.",
      );
      expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();

      refuse = false;
      await user.click(screen.getByRole("button", { name: "Test crm" }));
      await waitFor(() => {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      });
    });
  });
});
