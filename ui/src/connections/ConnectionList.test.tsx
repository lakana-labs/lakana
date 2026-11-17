import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionEnvironmentOut, ConnectionLastTestOut, ConnectionOut } from "../api";
import { aConnection, connectionRoutes, ENVIRONMENTS_ROUTE } from "../test/connections";
import { held } from "../test/pending";
import { fakeServer, type FakeRoute } from "../test/server";
import { MeContext } from "../shell/me";
import { ConnectionList } from "./ConnectionList";
import { STUCK_AFTER_MS } from "./useOpenTests";

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

/**
 * A route that answers the connections given, one per read, and repeats the last.
 *
 * @param answers - The connections, in order.
 * @returns The route.
 */
function inTurn(...answers: ConnectionOut[]): FakeRoute {
  let reads = 0;
  return () => {
    const body = answers[Math.min(reads, answers.length - 1)];
    reads += 1;
    return { status: 200, body };
  };
}

/**
 * Moves the fake clock on and lets the answers arrive.
 *
 * @param ms - The milliseconds.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/**
 * A finished test, as a manager reads it, done a moment ago.
 *
 * @param outcome - What it found.
 * @returns The test.
 */
function finished(outcome: "ok" | "refused"): ConnectionLastTestOut {
  const now = new Date().toISOString();
  return { id: "t9", state: "done", outcome, message: null, created_at: now, finished_at: now };
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
          created_at: new Date().toISOString(),
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
  describe("following open tests", () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    it.each([
      [
        "a manager",
        aConnection({
          last_test: {
            id: "t1",
            state: "running",
            outcome: null,
            message: null,
            created_at: new Date().toISOString(),
            finished_at: null,
          },
        }),
        MANAGER,
      ],
      [
        "a reader",
        aConnection({
          actions: [],
          last_test: { state: "running", outcome: null, finished_at: null },
        }),
        [{ name: "prod", actions: [] }] satisfies ConnectionEnvironmentOut[],
      ],
    ])(
      "resumes following an open test after a reload and never posts a test, as %s",
      async (_, running, environments) => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        const server = fakeServer({
          ...connectionRoutes([running], environments),
          "GET /api/v1/environments/prod/connections/crm": inTurn(running, {
            ...running,
            last_test: { state: "done", outcome: "ok", finished_at: new Date().toISOString() },
          }),
        });
        renderList();

        const row = (await screen.findAllByRole("row"))[1] as HTMLElement;
        expect(within(row).getByText("testing…")).toBeInTheDocument();
        expect(row.querySelector(".status-glyph-running")).not.toBeNull();
        const test = within(row).queryByRole("button", { name: "Test crm" });
        if (running.actions.includes("test")) {
          expect(test).toBeDisabled();
          expect(test).toHaveTextContent("Testing…");
        } else {
          expect(test).toBeNull();
        }

        await advance(1_000);
        expect(within(row).getByText("testing…")).toBeInTheDocument();
        await advance(1_000);
        await waitFor(() => {
          expect(row).toHaveTextContent("OK · just now");
        });
        await advance(30_000);

        const reads = server.sent.filter((request) =>
          request.path.startsWith("/api/v1/environments/prod/connections/crm"),
        );
        expect(reads).toHaveLength(2);
        expect(reads.every((request) => request.headers["x-lakana-background"] === "1")).toBe(true);
        expect(server.sent.some((request) => request.method !== "GET")).toBe(false);
      },
    );

    it("Test all in dev shows only with one environment chosen whose actions include test", async () => {
      fakeServer(
        connectionRoutes(
          [aConnection(), aConnection({ name: "files", environment: "dev" })],
          [
            { name: "dev", actions: ["test"] },
            { name: "prod", actions: [] },
          ],
        ),
      );
      const user = userEvent.setup();
      renderList();
      await screen.findByRole("link", { name: "files" });

      expect(
        screen.getByText("Choose an environment to test all of its connections."),
      ).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Test all/ })).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Environment: All environments" }));
      await user.click(screen.getByRole("option", { name: "dev" }));
      expect(await screen.findByRole("button", { name: "Test all in dev" })).toBeInTheDocument();
      expect(
        screen.queryByText("Choose an environment to test all of its connections."),
      ).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Environment: dev" }));
      await user.click(screen.getByRole("option", { name: "prod" }));
      await screen.findByRole("link", { name: "crm" });
      expect(screen.queryByRole("button", { name: /^Test all/ })).not.toBeInTheDocument();
      expect(
        screen.queryByText("Choose an environment to test all of its connections."),
      ).not.toBeInTheDocument();
    });

    it("Test all marks the rows open and counts them once every one is done, off screen too", async () => {
      window.localStorage.setItem("lakana.connections.kim.lee", "dev");
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const a = aConnection({ name: "a", environment: "dev" });
      const b = aConnection({ name: "b", environment: "dev", last_test: null });
      const server = fakeServer({
        ...connectionRoutes([a, b], MANAGER),
        "POST /api/v1/environments/dev/connection-tests": {
          status: 200,
          body: {
            items: ["a", "b", "c"].map((name) => ({
              connection: name,
              test_id: `t-${name}`,
              queued: true,
            })),
            next_cursor: null,
          },
        },
        "GET /api/v1/environments/dev/connections/a": {
          status: 200,
          body: { ...a, last_test: finished("ok") },
        },
        "GET /api/v1/environments/dev/connections/b": {
          status: 200,
          body: { ...b, last_test: finished("refused") },
        },
        "GET /api/v1/environments/dev/connections/c": {
          status: 200,
          body: aConnection({ name: "c", environment: "dev", last_test: finished("ok") }),
        },
      });
      const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
      renderList();

      await user.click(await screen.findByRole("button", { name: "Test all in dev" }));
      const status = screen.getByRole("status");
      await waitFor(() => {
        expect(status).toHaveTextContent("Testing 3 connections in dev…");
      });
      expect(screen.getAllByText("queued")).toHaveLength(2);
      expect(screen.getByRole("button", { name: "Test a" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Test b" })).toBeDisabled();

      await advance(1_000);
      await waitFor(() => {
        expect(status).toHaveTextContent("Tested 3 connections in dev: 2 OK, 1 not OK.");
      });
      expect(screen.getByRole("button", { name: "Test b" })).toBeEnabled();
      expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
    });

    it("says a test open for ten minutes is stuck, and offers Test again", async () => {
      fakeServer(
        connectionRoutes(
          [
            aConnection({
              last_test: {
                id: "t1",
                state: "running",
                outcome: null,
                message: null,
                created_at: new Date(Date.now() - STUCK_AFTER_MS).toISOString(),
                finished_at: null,
              },
            }),
          ],
          MANAGER,
        ),
      );
      renderList();

      expect(
        await screen.findByText(
          "No worker finished this test within 10 minutes; Test again closes it as failed and starts a new one.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
    });

    it("stops after three failed reads and Try again follows on, focus in the list", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const running = aConnection({
        last_test: { state: "running", outcome: null, finished_at: null },
      });
      const server = fakeServer({
        ...connectionRoutes([running], MANAGER),
        "GET /api/v1/environments/prod/connections/crm": {
          status: 503,
          body: { detail: "busy", type: "Busy" },
        },
      });
      const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
      renderList();
      await screen.findByText("testing…");

      await advance(1_000);
      await advance(5_000);
      await advance(5_000);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Could not read the test's progress.",
      );

      server.route({
        "GET /api/v1/environments/prod/connections/crm": {
          status: 200,
          body: { ...running, last_test: finished("ok") },
        },
      });
      await user.click(screen.getByRole("button", { name: "Try again" }));

      await waitFor(() => {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      });
      expect(screen.getByRole("region", { name: "Connections" })).toHaveFocus();
      await waitFor(() => {
        expect(screen.getAllByRole("row")[1]).toHaveTextContent("OK · just now");
      });
    });

    it("forgets a test it marked open when the filter changes, and shows the list's answer", async () => {
      const server = fakeServer({
        ...connectionRoutes([aConnection()], MANAGER),
        "POST /api/v1/environments/prod/connections/crm/tests": {
          status: 202,
          body: {
            id: "t1",
            state: "queued",
            outcome: null,
            message: null,
            created_at: new Date().toISOString(),
            finished_at: null,
          },
        },
      });
      const user = userEvent.setup();
      renderList();

      await user.click(await screen.findByRole("button", { name: "Test crm" }));
      expect(await screen.findByText("queued")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Environment: All environments" }));
      await user.click(screen.getByRole("option", { name: "prod" }));

      await waitFor(() => {
        expect(screen.queryByText("queued")).not.toBeInTheDocument();
      });
      await waitFor(() => {
        expect(screen.getAllByRole("row")[1]).toHaveTextContent(/OK · .* ago/);
      });
      expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
      expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
    });
  });
});
