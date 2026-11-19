import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionOut } from "../api";
import { aConnection, READABLE_ROUTE, readableRoute } from "../test/connections";
import { held } from "../test/pending";
import { fakeServer } from "../test/server";
import { ConnectionPage } from "./ConnectionPage";

const CRM = "GET /api/v1/environments/prod/connections/crm";
const DAY = 24 * 60 * 60 * 1000;

/**
 * Says the state of the address it is at.
 *
 * @returns The state, as text.
 */
function Probe(): JSX.Element {
  return <output data-testid="state">{JSON.stringify(useLocation().state)}</output>;
}

/**
 * Renders the page of `prod.crm`, as the screen's route does.
 *
 * @param state - The state of the address, as Add or Edit sends it.
 */
function renderPage(state?: unknown): void {
  render(
    <MemoryRouter initialEntries={[{ pathname: "/connections/prod/crm", state }]}>
      <Probe />
      <Routes>
        <Route
          path="/connections/prod/crm"
          element={<ConnectionPage environment="prod" name="crm" />}
        />
        <Route path="/connections" element={<p>the list</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Serves one connection and renders its page.
 *
 * @param connection - The connection.
 * @param state - The state of the address.
 * @returns The fake server.
 */
function showing(connection: ConnectionOut, state?: unknown): ReturnType<typeof fakeServer> {
  const server = fakeServer({ [CRM]: { status: 200, body: connection } });
  renderPage(state);
  return server;
}

describe("ConnectionPage", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says that it could not read the test's progress after three failed reads, and follows again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let reads = 0;
    fakeServer({
      [CRM]: () => {
        reads += 1;
        return reads === 1
          ? {
              status: 200,
              body: aConnection({
                last_test: {
                  id: "t1",
                  state: "running",
                  outcome: null,
                  message: null,
                  created_at: new Date().toISOString(),
                  finished_at: null,
                },
              }),
            }
          : reads <= 4
            ? { status: 500, body: { detail: "The catalog database is not reachable." } }
            : { status: 200, body: aConnection() };
      },
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPage();

    expect(await screen.findByText("testing…")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(15_000);

    expect(await screen.findByText("Could not read the test's progress.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await vi.advanceTimersByTimeAsync(1_000);

    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Last test" })).toHaveTextContent(/OK · /);
    });
    // The pressed button went away with the failure; focus is in the panel, not on the page.
    expect(document.body).not.toHaveFocus();
    expect(screen.getByRole("region", { name: "Last test" }).parentElement).toHaveFocus();
  });

  it("shows the address, settings, secret name, CA fingerprints, who created it and when", async () => {
    showing(
      aConnection({
        settings: { user: "reader", tls: "verify-full", ca_certificate: "sha256:ab12cd34" },
      }),
    );

    expect(await screen.findByRole("heading", { level: 1, name: "crm" })).toBeInTheDocument();
    const facts = screen.getByRole("region", { name: "Connection" });
    expect(within(facts).getByText("PostgreSQL")).toBeInTheDocument();
    expect(within(facts).getByText("db.example.org")).toBeInTheDocument();
    expect(within(facts).getByText("5432")).toBeInTheDocument();
    expect(within(facts).getByText("reader")).toBeInTheDocument();
    expect(within(facts).getByText("CA certificate")).toBeInTheDocument();
    expect(within(facts).getByText("sha256:ab12cd34")).toBeInTheDocument();
    expect(within(facts).getByText("crm-password (its value is never shown)")).toBeInTheDocument();
    expect(within(facts).getByText(/ana\.nowak, .*2026/)).toBeInTheDocument();
    expect(within(facts).getByText("Updated").nextSibling).toHaveTextContent(/2026/);
  });

  it("says none for a connection without a secret and lists a list of fingerprints", async () => {
    showing(
      aConnection({
        secret: null,
        settings: { ca_certificate: ["sha256:aa", "sha256:bb"], extra: { nested: true } },
      }),
    );

    const facts = await screen.findByRole("region", { name: "Connection" });
    expect(within(facts).getByText("none")).toBeInTheDocument();
    expect(within(facts).getByText("sha256:aa")).toBeInTheDocument();
    expect(within(facts).getByText("sha256:bb")).toBeInTheDocument();
    expect(within(facts).getByText('{"nested":true}')).toBeInTheDocument();
  });

  it("shows tls and auth in words and an unknown value as itself, marked", async () => {
    showing(
      aConnection({
        kind: "http",
        address: { base_url: "https://api.example.org" },
        settings: { tls: "verify-full", auth: "kerberos‮" },
        secret: null,
      }),
    );

    const facts = await screen.findByRole("region", { name: "Connection" });
    expect(within(facts).getByText("encrypted, certificate checked")).toBeInTheDocument();
    expect(within(facts).getByText("kerberos")).toBeInTheDocument();
    expect(facts).toHaveTextContent("U+202E");
  });

  it("says auth in words when it is known", async () => {
    showing(
      aConnection({
        kind: "http",
        address: { base_url: "https://api.example.org" },
        settings: { auth: "bearer" },
      }),
    );

    expect(await screen.findByText("bearer token")).toBeInTheDocument();
  });

  it("lists the declaring assets as plain text", async () => {
    showing(
      aConnection({
        declared_by: [
          { repository: "sales", asset: "orders" },
          { repository: "sales", asset: "customers" },
        ],
      }),
    );

    const used = await screen.findByRole("region", { name: "Used by" });
    expect(within(used).getByText("sales: orders")).toBeInTheDocument();
    expect(within(used).getByText("sales: customers")).toBeInTheDocument();
    expect(within(used).queryByRole("link")).not.toBeInTheDocument();
  });

  it("says that no asset declares it", async () => {
    showing(aConnection());

    expect(await screen.findByText("No deployed asset declares it.")).toBeInTheDocument();
  });

  it("a manager sees the last test's message and who ran it, a reader its cause and next step only", async () => {
    showing(
      aConnection({
        last_test: {
          id: "t1",
          state: "done",
          outcome: "refused",
          message: "connect ECONNREFUSED 10.0.0.5:5432",
          created_at: "2026-11-02T09:30:00Z",
          finished_at: "2026-11-02T09:30:02Z",
          requested_by: "ana.nowak",
        },
      }),
    );

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test).toHaveTextContent("refused");
    expect(within(test).getByText("The host refused the connection.")).toBeInTheDocument();
    expect(within(test).getByText("Check the port and the source's firewall.")).toBeVisible();
    expect(within(test).getByText("connect ECONNREFUSED 10.0.0.5:5432")).toBeInTheDocument();
    expect(within(test).getByText("by ana.nowak")).toBeInTheDocument();
    expect(within(test).getByText("lakana connection test 'crm' --env 'prod'")).toBeInTheDocument();
  });

  it("a reader sees the cause and next step, and no message or requester", async () => {
    showing(
      aConnection({
        last_test: { state: "done", outcome: "quota", finished_at: new Date().toISOString() },
        updated_at: "2026-11-02T09:00:00Z",
        actions: [],
      } as unknown as Partial<ConnectionOut>),
    );

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test).toHaveTextContent("failed · ");
    expect(
      within(test).getByText(
        "The check failed in another way, or no worker finished it within 10 minutes.",
      ),
    ).toBeInTheDocument();
    expect(within(test).queryByText(/^by /)).not.toBeInTheDocument();
    expect(test.querySelector(".connection-message")).toBeNull();
  });

  it("a stale last test says to test again", async () => {
    showing(
      aConnection({
        updated_at: "2026-11-05T09:00:00Z",
        last_test: {
          id: "t1",
          state: "done",
          outcome: "refused",
          message: null,
          created_at: "2026-11-02T09:30:00Z",
          finished_at: "2026-11-02T09:30:02Z",
          requested_by: "ana.nowak",
        },
      }),
    );

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test).toHaveTextContent("tested before the last change");
    expect(within(test).getByText("Tested before the last change; test it again.")).toBeVisible();
    expect(within(test).queryByText("The host refused the connection.")).not.toBeInTheDocument();
  });

  it("says never tested for a connection with no test", async () => {
    showing(aConnection({ last_test: null }));

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test).toHaveTextContent("never tested");
  });

  it("says a successful test with its full date and no cause", async () => {
    showing(aConnection());

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test).toHaveTextContent(/OK · .*2026/);
    expect(within(test).queryByText("The source can be reached.")).not.toBeInTheDocument();
  });

  it("offers Test, Edit and Delete only by actions", async () => {
    fakeServer({
      [CRM]: { status: 200, body: aConnection({ actions: ["test"] }) },
    });
    renderPage();

    expect(await screen.findByRole("button", { name: "Test crm" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete…" })).not.toBeInTheDocument();
  });

  it("gives a manager Test as the primary action, Edit as a link and Delete…", async () => {
    showing(aConnection());

    const test = await screen.findByRole("button", { name: "Test crm" });
    expect(test).toHaveClass("button-primary");
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute(
      "href",
      "/connections/prod/crm/edit",
    );
    expect(screen.getByRole("button", { name: "Delete…" })).not.toHaveClass("button-primary");
  });

  it("marks its environment when it is protected, and names it plainly until that is known", async () => {
    const server = fakeServer({
      [CRM]: { status: 200, body: aConnection() },
      [READABLE_ROUTE]: readableRoute(["dev", "prod"], ["prod"]),
    });
    renderPage();

    await screen.findByRole("button", { name: "Test crm" });
    const label = document.querySelector(".connection-actions .env-label");
    await waitFor(() => {
      expect(label).toHaveTextContent("Environment ● prod · protected");
    });
    expect(label?.querySelector(".env-protected")).not.toBeNull();
    expect(server.sent.filter((request) => request.path === "/api/v1/environments")).toHaveLength(
      1,
    );
  });

  it("names its environment plainly when it is not protected or cannot be told", async () => {
    showing(aConnection());

    await screen.findByRole("button", { name: "Test crm" });
    expect(document.querySelector(".connection-actions .env-label")).toHaveTextContent(
      /^Environment prod$/u,
    );
  });

  it("gives a reader no action", async () => {
    showing(aConnection({ actions: [] }));

    await screen.findByRole("region", { name: "Last test" });
    expect(screen.queryByRole("button", { name: /Test|Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("the breadcrumb is Connections › name", async () => {
    showing(aConnection());

    const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByRole("link", { name: "Connections" })).toHaveAttribute(
      "href",
      "/connections",
    );
    expect(within(trail).getByText("crm")).toHaveAttribute("aria-current", "page");
  });

  it("a missing connection shows the not-found state with a link to the list", async () => {
    fakeServer({
      [CRM]: { status: 404, body: { detail: "No connection crm in prod.", type: "NotFound" } },
    });
    renderPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Connections" })).toHaveAttribute(
      "href",
      "/connections",
    );
  });

  it("says why the connection could not be read, and reads it again", async () => {
    let fail = true;
    fakeServer({
      [CRM]: () =>
        fail
          ? { status: 500, body: { detail: "The catalog database is not reachable." } }
          : { status: 200, body: aConnection() },
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The catalog database is not reachable.",
    );
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("heading", { name: "crm" })).toBeInTheDocument();
  });

  it("after a create the note is focused and Test now is the primary action", async () => {
    showing(aConnection({ last_test: null }), { created: true });

    const note = await screen.findByText(
      "Created crm. Test it now to see that a worker can reach it.",
    );
    expect(note).toHaveAttribute("role", "status");
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    const test = screen.getByRole("button", { name: "Test now" });
    expect(test).toHaveClass("button-primary");
  });

  it("after a save the note says so, and Test is plain Test", async () => {
    showing(aConnection(), { saved: true });

    expect(await screen.findByText("Saved crm.")).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Test crm" })).toBeInTheDocument();
  });

  it("follows a running test for a reader and shows the outcome with its full date", async () => {
    let reads = 0;
    const running = {
      state: "running",
      outcome: null,
      finished_at: null,
    };
    fakeServer({
      [CRM]: () => {
        reads += 1;
        return {
          status: 200,
          body: aConnection({
            actions: [],
            last_test:
              reads < 3
                ? (running as unknown as ConnectionOut["last_test"])
                : { state: "done", outcome: "ok", finished_at: new Date().toISOString() },
            updated_at: "2026-11-02T09:00:00Z",
          }),
        };
      },
    });
    renderPage();

    expect(await screen.findByText("testing…")).toBeInTheDocument();
    await waitFor(
      () => {
        expect(screen.getByRole("region", { name: "Last test" })).toHaveTextContent(/OK · /);
      },
      { timeout: 6000 },
    );
  }, 10_000);

  it("says that Test again closes a test open for ten minutes", async () => {
    const created = new Date(Date.now() - 11 * 60_000).toISOString();
    showing(
      aConnection({
        last_test: {
          id: "t1",
          state: "running",
          outcome: null,
          message: null,
          created_at: created,
          finished_at: null,
          requested_by: "ana.nowak",
        },
      }),
    );

    expect(await screen.findByText(/No worker finished this test within 10 minutes/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
  });

  it("shows the not-found state when the connection is deleted while it is open", async () => {
    let reads = 0;
    fakeServer({
      [CRM]: () => {
        reads += 1;
        return reads === 1
          ? {
              status: 200,
              body: aConnection({
                last_test: {
                  id: "t1",
                  state: "running",
                  outcome: null,
                  message: null,
                  created_at: new Date().toISOString(),
                  finished_at: null,
                },
              }),
            }
          : { status: 404, body: { detail: "No connection crm in prod.", type: "NotFound" } };
      },
    });
    renderPage();

    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "This page does not exist" },
        { timeout: 4000 },
      ),
    ).toBeInTheDocument();
  }, 8000);

  it("puts the focus on the not-found heading when following finds the connection deleted", async () => {
    let reads = 0;
    fakeServer({
      [CRM]: () => {
        reads += 1;
        return reads === 1
          ? {
              status: 200,
              body: aConnection({
                last_test: {
                  id: "t1",
                  state: "running",
                  outcome: null,
                  message: null,
                  created_at: new Date().toISOString(),
                  finished_at: null,
                },
              }),
            }
          : { status: 404, body: { detail: "No connection crm in prod.", type: "NotFound" } };
      },
    });
    renderPage();

    const heading = await screen.findByRole(
      "heading",
      { level: 1, name: "This page does not exist" },
      { timeout: 4000 },
    );
    expect(heading).toHaveFocus();
  }, 8000);

  it("long past a test's start, the day is in the full date", async () => {
    const finished = new Date(Date.now() - 3 * DAY).toISOString();
    showing(
      aConnection({
        updated_at: new Date(Date.now() - 4 * DAY).toISOString(),
        last_test: { state: "done", outcome: "ok", finished_at: finished },
      } as unknown as Partial<ConnectionOut>),
    );

    const test = await screen.findByRole("region", { name: "Last test" });
    expect(test.querySelector("time")).toHaveAttribute("datetime", finished);
  });

  it("holds the focus on the heading while it reads the connection after a create", async () => {
    const answer = held();
    fakeServer({ [CRM]: answer.route });
    renderPage({ created: true });

    const heading = await screen.findByRole("heading", { level: 1, name: "crm" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });

    answer.release({ status: 200, body: aConnection({ last_test: null }) });

    const note = await screen.findByText(/^Created crm\./);
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
  });

  it("drops the note from the address once it is shown, so a reload does not say it again", async () => {
    showing(aConnection(), { created: true });

    expect(await screen.findByText(/^Created crm\./)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("state")).toHaveTextContent("null");
    });
    expect(screen.getByText(/^Created crm\./)).toBeInTheDocument();
  });

  it("shows a kind this build does not know as itself, marked", async () => {
    showing(aConnection({ kind: "ftp\u202E" as ConnectionOut["kind"] }));

    const facts = await screen.findByRole("region", { name: "Connection" });
    expect(facts).toHaveTextContent("ftpU+202E");
  });

  it("marks a hidden character in every value it shows", async () => {
    const mark = "\u202E";
    showing(
      aConnection({
        name: "crm",
        secret: `pw${mark}`,
        created_by: `ana${mark}`,
        settings: { ca_certificate: `sha256:ab${mark}` },
        declared_by: [{ repository: `sales${mark}`, asset: `orders${mark}` }],
        last_test: {
          id: "t1",
          state: "done",
          outcome: "refused",
          message: `boom${mark}`,
          created_at: "2026-11-02T09:30:00Z",
          finished_at: "2026-11-02T09:30:02Z",
          requested_by: `bob${mark}`,
        },
      }),
    );

    await screen.findByRole("region", { name: "Last test" });
    const text = document.body.textContent;
    expect(text).not.toContain(mark);
    for (const marked of [
      "pwU+202E",
      "anaU+202E",
      "sha256:abU+202E",
      "sales",
      "boomU+202E",
      "bobU+202E",
    ]) {
      expect(text).toContain(marked);
    }
    expect(text).toContain("sales" + "U+202E: orders" + "U+202E");
  });
});
