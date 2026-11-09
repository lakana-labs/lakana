import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { describe, expect, it } from "vitest";

import { SessionProvider, useSession } from "./SessionProvider";

import { ADMIN_SESSION, fakeServer } from "../test/server";
import { MemoryStorage } from "../test/storage";

const SIGNED_OUT = {
  status: 401,
  body: { detail: "Sign in.", type: "AuthenticationError" },
};

/**
 * Shows the session value and offers its actions as buttons.
 *
 * @returns The probe.
 */
function Probe(): JSX.Element {
  const session = useSession();
  return (
    <div>
      <p>state: {session.state}</p>
      <p>name: {session.session?.name ?? "none"}</p>
      <p>reason: {session.reason ?? "none"}</p>
      <p>offered: {String(session.passkeyOffered)}</p>
      <button
        type="button"
        onClick={() => {
          session.signOut().catch((error: unknown) => {
            document.title = `failed: ${String(error)}`;
          });
        }}
      >
        out
      </button>
      <button type="button" onClick={() => void session.signOutEverywhere()}>
        everywhere
      </button>
      <button type="button" onClick={session.skipPasskey}>
        skip
      </button>
    </div>
  );
}

/**
 * Renders the provider around the probe.
 *
 * @param storage - Where the skipped passkey offer is kept.
 * @returns A user-event session.
 */
function renderProbe(
  storage: Storage | null = new MemoryStorage(),
): ReturnType<typeof userEvent.setup> {
  const user = userEvent.setup();
  render(
    <SessionProvider storage={storage}>
      <Probe />
    </SessionProvider>,
  );
  return user;
}

describe("SessionProvider", () => {
  it("loads the session", async () => {
    fakeServer({ "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION } });
    renderProbe();

    expect(screen.getByText("state: loading")).toBeInTheDocument();
    expect(await screen.findByText("state: signed-in")).toBeInTheDocument();
    expect(screen.getByText("name: admin")).toBeInTheDocument();
  });

  it("is signed out without a reason when the browser has no session", async () => {
    fakeServer({ "GET /api/v1/sign-in/session": SIGNED_OUT });
    renderProbe();

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(screen.getByText("reason: none")).toBeInTheDocument();
  });

  it("has no passkey offer to skip while signed out", async () => {
    fakeServer({ "GET /api/v1/sign-in/session": SIGNED_OUT });
    const storage = new MemoryStorage();
    const user = renderProbe(storage);
    await screen.findByText("state: signed-out");

    await user.click(screen.getByRole("button", { name: "skip" }));

    expect(storage.length).toBe(0);
    expect(screen.getByText("offered: false")).toBeInTheDocument();
  });

  it("signs out on a later 401 and keeps the reason", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/sign-in/session": {
        status: 401,
        body: { detail: "x", type: "SessionEndedError", reason: "ended_by_admin" },
      },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");

    await user.click(screen.getByRole("button", { name: "out" }));

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(
      screen.getByText("reason: An administrator ended your session. Sign in again."),
    ).toBeInTheDocument();
    expect(document.title).not.toMatch(/^failed/);
    expect(server.sent).toHaveLength(2);
  });

  it("says the person signed out, here or everywhere", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/sign-in/session": { status: 204 },
      "DELETE /api/v1/principals/admin/sessions": { status: 204 },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");

    await user.click(screen.getByRole("button", { name: "out" }));
    expect(await screen.findByText("reason: You signed out.")).toBeInTheDocument();

    server.route({ "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION } });
    render(
      <SessionProvider storage={null}>
        <Probe />
      </SessionProvider>,
    );
    const second = (await screen.findAllByText("state: signed-in"))[0];
    expect(second).toBeDefined();
    await user.click(screen.getAllByRole("button", { name: "everywhere" })[1] as HTMLElement);
    expect(await screen.findByText("reason: You signed out on every device.")).toBeInTheDocument();
  });

  it("stays signed in when signing out fails, and passes the failure on", async () => {
    fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/sign-in/session": { status: 503, body: { detail: "Busy.", type: "Busy" } },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");

    await user.click(screen.getByRole("button", { name: "out" }));

    await act(() => Promise.resolve());
    expect(document.title).toBe("failed: ApiError: Busy.");
    expect(screen.getByText("state: signed-in")).toBeInTheDocument();
  });

  it("offers a passkey once per session", async () => {
    const offered = { ...ADMIN_SESSION, via: "local", offer_passkey: true };
    fakeServer({ "GET /api/v1/sign-in/session": { status: 200, body: offered } });
    const storage = new MemoryStorage();
    const user = renderProbe(storage);
    expect(await screen.findByText("offered: true")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "skip" }));

    expect(screen.getByText("offered: false")).toBeInTheDocument();
    render(
      <SessionProvider storage={storage}>
        <Probe />
      </SessionProvider>,
    );
    await screen.findAllByText("state: signed-in");
    expect(screen.getAllByText("offered: false")).toHaveLength(2);
  });

  it("offers again for a new session", async () => {
    const storage = new MemoryStorage();
    storage.setItem("lakana.passkey-offer-skipped", "admin 2026-01-01T00:00:00Z");
    fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { ...ADMIN_SESSION, via: "local", offer_passkey: true },
      },
    });
    renderProbe(storage);

    expect(await screen.findByText("offered: true")).toBeInTheDocument();
  });

  it.each([
    ["none", null],
    [
      "one that refuses",
      {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      } as unknown as Storage,
    ],
  ])("works without browser storage (%s)", async (_, storage) => {
    fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { ...ADMIN_SESSION, via: "local", offer_passkey: true },
      },
    });
    const user = renderProbe(storage);
    await screen.findByText("offered: true");

    await user.click(screen.getByRole("button", { name: "skip" }));

    expect(screen.getByText("offered: false")).toBeInTheDocument();
  });

  it("refuses to be used outside its provider", () => {
    expect(() => render(<Probe />)).toThrow(/inside <SessionProvider>/);
  });
});
