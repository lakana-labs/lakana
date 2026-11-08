import { render, screen, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Interface, type RouterFactory } from "./Interface";

import { PreferencesProvider } from "../preferences/PreferencesProvider";
import {
  ADMIN_SESSION,
  type FakeRoute,
  type FakeServer,
  fakeServer,
  permissions,
} from "../test/server";
import { MemoryStorage } from "../test/storage";
import { fakeWebAuthn } from "../test/webauthn";

const SIGNED_OUT = {
  status: 401,
  body: { detail: "Sign in to use this.", type: "AuthenticationError" },
};
const OPTIONS = { status: 200, body: { ceremony_id: "c-1", public_key: { challenge: "abc" } } };

/** What {@link renderInterface} hands back. */
interface Rendered {
  /** The fake server. */
  readonly server: FakeServer;
  /** A user-event session. */
  readonly user: UserEvent;
  /** The path the newest router shows. */
  readonly path: () => string;
}

/**
 * Renders the whole interface at a path in memory routers.
 *
 * @param path - The path to start at.
 * @param routes - The fake server's routes beyond notices and an administrator's rights.
 * @param storage - The browser storage.
 * @returns The server, a user-event session and the current path.
 */
function renderInterface(
  path: string,
  routes: Record<string, FakeRoute>,
  storage: Storage = new MemoryStorage(),
): Rendered {
  const server = fakeServer({
    "GET /api/v1/sign-in/notices": { status: 200, body: [] },
    "GET /api/v1/principals/admin/permissions": { status: 200, body: permissions(true) },
    ...routes,
  });
  let newest: ReturnType<typeof createMemoryRouter> | null = null;
  const createRouter: RouterFactory = (tree, at) => {
    newest = createMemoryRouter(tree, { initialEntries: [at ?? path] });
    return newest;
  };
  const user = userEvent.setup();
  render(
    <PreferencesProvider storage={storage}>
      <Interface createRouter={createRouter} storage={storage} />
    </PreferencesProvider>,
  );
  return {
    server,
    user,
    path: () => (newest === null ? "" : newest.state.location.pathname),
  };
}

/**
 * A session route whose answer a test can change.
 *
 * @param first - The first answer.
 * @param first.status - Its status.
 * @param first.body - Its body.
 * @returns The route and a setter for the next answers.
 */
function sessionRoute(first: { status: number; body: unknown }): {
  route: () => { status: number; body: unknown };
  next: (answer: { status: number; body: unknown }) => void;
} {
  let current = first;
  return {
    route: () => current,
    next: (answer) => {
      current = answer;
    },
  };
}

/**
 * The sent requests as "METHOD path".
 *
 * @param server - The fake server.
 * @returns One line per request.
 */
function lines(server: FakeServer): string[] {
  return server.sent.map((request) => `${request.method} ${request.path}`);
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("Interface", () => {
  it("removes the link secret from the address bar before any request", async () => {
    window.history.replaceState(null, "", "/sign-in#l=pub-1&s=sec-1");
    const events: string[] = [];
    const replace = vi.spyOn(window.history, "replaceState");
    replace.mockImplementation(function (this: History, ...args) {
      events.push("replaceState");
      History.prototype.replaceState.apply(this, args);
    });
    const session = sessionRoute(SIGNED_OUT);
    const { server, user } = renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": (request) => {
        events.push(request.path);
        return session.route();
      },
      "POST /api/v1/sign-in/links/describe": (request) => {
        events.push(request.path);
        return {
          status: 200,
          body: { kind: "invite", name: "kim", display_name: null, state: "open" },
        };
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 200,
        body: { name: "kim", needs_passkey: true, offer_passkey: false, csrf_token: "c" },
      },
    });

    await screen.findByRole("heading", { level: 1, name: "Join Lakana as kim" });

    expect(events[0]).toBe("replaceState");
    expect(window.location.href).not.toContain("sec-1");
    session.next({ status: 200, body: { ...ADMIN_SESSION, name: "kim", needs_passkey: true } });
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("heading", { level: 1, name: "Add a passkey" });
    const redeem = server.sent.find((request) => request.path.endsWith("/redeem"));
    expect(redeem?.body).toEqual({ link_id: "pub-1", secret: "sec-1" });
    expect(server.sent.filter((request) => request.path.includes("sec-1"))).toEqual([]);
    replace.mockRestore();
  });

  it("never uses a link on open; only the confirm button does", async () => {
    window.history.replaceState(null, "", "/sign-in#l=pub-1&s=sec-1");
    const session = sessionRoute(SIGNED_OUT);
    const { server, user } = renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": session.route,
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "recovery", name: "admin", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 200,
        body: { name: "admin", needs_passkey: false, offer_passkey: false, csrf_token: "c" },
      },
    });

    const heading = await screen.findByRole("heading", { level: 1, name: "Sign in as admin" });
    expect(heading).toHaveFocus();
    expect(screen.getByText(/^This link signs you in once/)).toBeInTheDocument();
    expect(lines(server).filter((line) => line.endsWith("/redeem"))).toEqual([]);

    session.next({ status: 200, body: ADMIN_SESSION });
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(lines(server).filter((line) => line.endsWith("/redeem"))).toHaveLength(1);
  });

  it("uses a local link at once only when no session exists", async () => {
    window.history.replaceState(null, "", "/sign-in#l=pub-1&s=sec-1");
    const session = sessionRoute(SIGNED_OUT);
    const { server } = renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": session.route,
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "local", name: "admin", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": () => {
        session.next({ status: 200, body: ADMIN_SESSION });
        return {
          status: 200,
          body: { name: "admin", needs_passkey: false, offer_passkey: false, csrf_token: "c" },
        };
      },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(lines(server).filter((line) => line.endsWith("/redeem"))).toHaveLength(1);
  });

  it("asks before using a local link in a browser that is signed in", async () => {
    window.history.replaceState(null, "", "/sign-in#l=pub-1&s=sec-1");
    const { server, user } = renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "local", name: "admin", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 409,
        body: { detail: "Already signed in.", type: "ConflictError" },
      },
    });

    await screen.findByRole("heading", { level: 1, name: "Sign in as admin" });
    expect(lines(server).filter((line) => line.endsWith("/redeem"))).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^This browser is already signed in, so the local sign-in link was not used/,
    );
    await user.click(screen.getByRole("button", { name: "Go to Home" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
  });

  it.each([
    ["used", "This link was already used", /^Each sign-in link works once/],
    ["expired", "This link has expired", /^Ask whoever sent it for a new link/],
    ["withdrawn", "This link was withdrawn", /^Ask whoever sent it for a new link/],
    ["unknown", "This link does not work", /^Check that you opened all of it/],
  ])("names a %s link with the next step", async (state, title, body) => {
    window.history.replaceState(null, "", "/sign-in#l=pub-1&s=sec-1");
    const { user, path } = renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": SIGNED_OUT,
      "POST /api/v1/sign-in/links/describe": {
        status: 410,
        body: { detail: "x", type: "LinkUnusableError", state },
      },
    });

    expect(await screen.findByRole("heading", { level: 1, name: title })).toHaveFocus();
    expect(screen.getByText(body)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Go to sign-in" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in to Lakana" }),
    ).toBeInTheDocument();
    expect(path()).toBe("/sign-in");
  });

  it("signs out to the sign-in page on 401 with the reason", async () => {
    const { path } = renderInterface("/catalog", {
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "GET /api/v1/sign-in/notices": {
        status: 401,
        body: { detail: "Ended.", type: "SessionEndedError", reason: "idle" },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in to Lakana" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Your session ended after a time without activity. Sign in again."),
    ).toBeInTheDocument();
    expect(path()).toBe("/sign-in");
  });

  it("returns to the page the person wanted after a passkey sign-in", async () => {
    fakeWebAuthn();
    const session = sessionRoute(SIGNED_OUT);
    const { user, path } = renderInterface("/jobs", {
      "GET /api/v1/sign-in/session": session.route,
      "POST /api/v1/sign-in/passkey/options": OPTIONS,
      "POST /api/v1/sign-in/passkey": () => {
        session.next({ status: 200, body: ADMIN_SESSION });
        return {
          status: 200,
          body: { name: "admin", needs_passkey: false, offer_passkey: false, csrf_token: "c" },
        };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Sign in with a passkey" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(path()).toBe("/jobs");
  });

  it("sends a signed-in browser from the sign-in page to Home", async () => {
    renderInterface("/sign-in", {
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
  });

  it("says it is loading until the session is known", async () => {
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    renderInterface("/", {
      "GET /api/v1/sign-in/session": () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });

    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
    answer({ status: 200, body: ADMIN_SESSION });
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
  });

  it.each(["setup", "invite", "recovery"])("asks for a passkey after a %s link", async (via) => {
    const webauthn = fakeWebAuthn();
    const session = sessionRoute({
      status: 200,
      body: { ...ADMIN_SESSION, via, needs_passkey: true },
    });
    const { server, user } = renderInterface("/catalog", {
      "GET /api/v1/sign-in/session": session.route,
      "POST /api/v1/principals/admin/passkeys/options": OPTIONS,
      "POST /api/v1/principals/admin/passkeys": () => {
        session.next({ status: 200, body: { ...ADMIN_SESSION, via } });
        return { status: 201, body: { id: "p-1" } };
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Add a passkey" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Not now" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(lines(server)).toEqual(["GET /api/v1/sign-in/session"]);

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" })).toBeInTheDocument();
    expect(webauthn.create).toHaveBeenCalledTimes(1);
  });

  it("offers, never forces, a passkey after a local link", async () => {
    const storage = new MemoryStorage();
    const offered = { ...ADMIN_SESSION, via: "local", offer_passkey: true };
    const { user } = renderInterface(
      "/",
      { "GET /api/v1/sign-in/session": { status: 200, body: offered } },
      storage,
    );

    expect(
      await screen.findByRole("heading", { level: 1, name: "Add a passkey" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not now" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Signed in as admin" })).toBeInTheDocument();
  });

  it("hides Access and Settings from a person without admin rights", async () => {
    renderInterface("/", {
      "GET /api/v1/sign-in/session": { status: 200, body: { ...ADMIN_SESSION, name: "kim" } },
      "GET /api/v1/principals/kim/permissions": { status: 200, body: permissions(false, true) },
    });

    const navigation = await screen.findByRole("navigation");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    const names = within(navigation)
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(names).toEqual(["Home", "Catalog", "Queries", "Jobs", "Connections"]);
    expect(screen.queryByText(/no access yet/)).not.toBeInTheDocument();
  });

  it("shows Access and Settings to an administrator", async () => {
    renderInterface("/", { "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION } });

    const navigation = await screen.findByRole("navigation");
    expect(within(navigation).getByRole("link", { name: "Access" })).toBeInTheDocument();
    expect(within(navigation).getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  it("shows 'You have no access yet; ask an administrator' on an empty Home", async () => {
    renderInterface("/", {
      "GET /api/v1/sign-in/session": { status: 200, body: { ...ADMIN_SESSION, name: "kim" } },
      "GET /api/v1/principals/kim/permissions": { status: 200, body: permissions(false) },
      "GET /api/v1/environments": { status: 200, body: [] },
    });

    expect(
      await screen.findByText("You have no access yet; ask an administrator."),
    ).toBeInTheDocument();
  });

  it("hides the administration areas when the rights cannot be read", async () => {
    renderInterface("/", {
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "GET /api/v1/principals/admin/permissions": {
        status: 503,
        body: { detail: "Busy.", type: "Busy" },
      },
    });

    const navigation = await screen.findByRole("navigation");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(within(navigation).queryByRole("link", { name: "Access" })).not.toBeInTheDocument();
  });
});
