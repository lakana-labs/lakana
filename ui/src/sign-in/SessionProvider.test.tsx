import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CSRF_STORAGE_KEY, onSessionEnd, setCsrfToken } from "../api/client";
import { SESSION_RETRY_FIRST_MS, SessionProvider, useSession } from "./SessionProvider";

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
      <button
        type="button"
        onClick={() => {
          document.title = "reading";
          void session.refresh().then(() => {
            document.title = "read";
          });
        }}
      >
        refresh
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

const BUSY = { status: 503, body: { detail: "Busy.", type: "Busy" } };

/** An hour before {@link ADMIN_SESSION} reaches its absolute end. */
const BEFORE_THE_END = new Date("2026-11-15T09:00:00Z");

/**
 * Lets the fake timers run on by some milliseconds, and React settle.
 *
 * @param ms - How far.
 */
async function wait(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("SessionProvider", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is signed out with the cause when the server cannot be read at start", async () => {
    fakeServer({ "GET /api/v1/sign-in/session": BUSY });
    renderProbe();

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(screen.getByText("reason: Busy.")).toBeInTheDocument();
  });

  it("stays signed in and reads again, in the background, when a read fails without a 401", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const ended = vi.fn();
    const stop = onSessionEnd(ended);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");
    const reads = (): typeof server.sent =>
      server.sent.filter((request) => request.path === "/api/v1/sign-in/session");

    server.route({ "GET /api/v1/sign-in/session": BUSY });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await act(() => Promise.resolve());
    expect(reads()).toHaveLength(2);
    expect(screen.getByText("state: signed-in")).toBeInTheDocument();
    expect(screen.getByText("name: admin")).toBeInTheDocument();

    // The wait doubles after each failure: 2 s, then 4 s.
    await wait(SESSION_RETRY_FIRST_MS);
    expect(reads()).toHaveLength(3);
    await wait(SESSION_RETRY_FIRST_MS);
    expect(reads()).toHaveLength(3);
    server.route({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { ...ADMIN_SESSION, offer_passkey: true },
      },
    });
    await wait(SESSION_RETRY_FIRST_MS);
    expect(reads()).toHaveLength(4);
    expect(await screen.findByText("offered: true")).toBeInTheDocument();
    expect(
      reads()
        .slice(2)
        .every((read) => read.headers["x-lakana-background"] === "1"),
    ).toBe(true);

    // Read well: no further reads.
    await wait(10 * SESSION_RETRY_FIRST_MS);
    expect(reads()).toHaveLength(4);
    expect(screen.getByText("state: signed-in")).toBeInTheDocument();
    expect(ended).not.toHaveBeenCalled();
    stop();
  });

  it("signs out and forgets what the page kept when a read again gets a 401", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const ended = vi.fn();
    const stop = onSessionEnd(ended);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");
    server.route({ "GET /api/v1/sign-in/session": BUSY });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await act(() => Promise.resolve());
    expect(screen.getByText("state: signed-in")).toBeInTheDocument();

    server.route({
      "GET /api/v1/sign-in/session": {
        status: 401,
        body: { detail: "x", type: "SessionEndedError", reason: "idle" },
      },
    });
    await wait(SESSION_RETRY_FIRST_MS);

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(ended).toHaveBeenCalledTimes(1);
    stop();
  });

  it.each([
    ["is read", { status: 200, body: ADMIN_SESSION }],
    ["fails", BUSY],
  ])("changes nothing when a read that %s answers after it went away", async (_, answer) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = userEvent.setup();
    const { unmount } = render(
      <SessionProvider storage={null}>
        <Probe />
      </SessionProvider>,
    );
    await screen.findByText("state: signed-in");
    let answerNow: (value: typeof answer) => void = () => undefined;
    server.route({
      "GET /api/v1/sign-in/session": () =>
        new Promise((resolve) => {
          answerNow = resolve;
        }),
    });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    const sent = server.sent.length;

    unmount();
    answerNow(answer);
    await wait(10 * SESSION_RETRY_FIRST_MS);

    expect(server.sent).toHaveLength(sent);
  });

  it("a person-started read waits through the reads again, until one lands", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");
    server.route({ "GET /api/v1/sign-in/session": BUSY });

    await user.click(screen.getByRole("button", { name: "refresh" }));
    await wait(0);
    expect(document.title).toBe("reading");
    await wait(SESSION_RETRY_FIRST_MS);
    expect(document.title).toBe("reading");

    server.route({ "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION } });
    await wait(2 * SESSION_RETRY_FIRST_MS);
    expect(document.title).toBe("read");
  });

  it("a person-started read ends when a read again gets a 401", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");
    server.route({ "GET /api/v1/sign-in/session": BUSY });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await wait(0);

    server.route({ "GET /api/v1/sign-in/session": SIGNED_OUT });
    await wait(SESSION_RETRY_FIRST_MS);

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(document.title).toBe("read");
  });

  it.each([
    ["a read again after a failure (another tab signed someone else in)", true],
    ["a read the person started (a sign-in link of someone else)", false],
  ])(
    "forgets what the page kept before it shows another person, after %s",
    async (_, failFirst) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(BEFORE_THE_END);
      const shownAtTheEnd: string[] = [];
      const stop = onSessionEnd(() => {
        shownAtTheEnd.push(screen.getByText(/^name: /).textContent);
      });
      const server = fakeServer({
        "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      });
      const user = renderProbe();
      await screen.findByText("name: admin");
      setCsrfToken("csrf-of-bob");
      const bob = { ...ADMIN_SESSION, name: "bob" };
      server.route({
        "GET /api/v1/sign-in/session": failFirst ? BUSY : { status: 200, body: bob },
      });

      await user.click(screen.getByRole("button", { name: "refresh" }));
      if (failFirst) {
        await wait(0);
        server.route({ "GET /api/v1/sign-in/session": { status: 200, body: bob } });
        await wait(SESSION_RETRY_FIRST_MS);
      }

      expect(await screen.findByText("name: bob")).toBeInTheDocument();
      expect(shownAtTheEnd).toEqual(["name: admin"]);
      // Bob's session is the live one: its token stays.
      expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-of-bob");
      stop();
    },
  );

  it("keeps what the page kept for a new session of the same person", async () => {
    const ended = vi.fn();
    const stop = onSessionEnd(ended);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("name: admin");
    server.route({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { ...ADMIN_SESSION, absolute_expires_at: "2026-11-16T10:00:00Z" },
      },
    });

    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => {
      expect(document.title).toBe("read");
    });

    expect(ended).not.toHaveBeenCalled();
    stop();
  });

  it.each([
    ["when the first read fails after the end", 0],
    ["at the end, while the reads again fail", SESSION_RETRY_FIRST_MS],
  ])(
    "ends the session here at its absolute end while the server cannot answer, %s",
    async (_, beforeTheEnd) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(new Date(Date.parse(ADMIN_SESSION.absolute_expires_at) - 60_000));
      const ended = vi.fn();
      const stop = onSessionEnd(ended);
      const server = fakeServer({
        "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      });
      setCsrfToken("csrf-1");
      const user = renderProbe();
      await screen.findByText("state: signed-in");
      vi.setSystemTime(new Date(Date.parse(ADMIN_SESSION.absolute_expires_at) - beforeTheEnd));
      server.route({ "GET /api/v1/sign-in/session": BUSY });

      await user.click(screen.getByRole("button", { name: "refresh" }));
      if (beforeTheEnd > 0) {
        await wait(0);
        expect(screen.getByText("state: signed-in")).toBeInTheDocument();
        // The next read waits no longer than the end.
        await wait(beforeTheEnd);
      }

      expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
      expect(
        screen.getByText(
          "reason: Your session reached its maximum length and ended. Sign in again.",
        ),
      ).toBeInTheDocument();
      expect(ended).toHaveBeenCalledTimes(1);
      expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
      expect(document.title).toBe("read");
      stop();
    },
  );

  it("at the absolute end keeps a newer token another tab stored", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const end = Date.parse(ADMIN_SESSION.absolute_expires_at);
    vi.setSystemTime(new Date(end - 60_000));
    setCsrfToken("csrf-1");
    const ended = vi.fn();
    const stop = onSessionEnd(ended);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = renderProbe();
    await screen.findByText("state: signed-in");
    vi.setSystemTime(new Date(end));
    // Another tab signed someone in meanwhile: its token is the live one.
    localStorage.setItem(CSRF_STORAGE_KEY, "csrf-2");
    server.route({ "GET /api/v1/sign-in/session": BUSY });

    await user.click(screen.getByRole("button", { name: "refresh" }));

    expect(await screen.findByText("state: signed-out")).toBeInTheDocument();
    expect(ended).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-2");
    stop();
  });

  it("a person-started read ends when the provider goes away", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = userEvent.setup();
    const { unmount } = render(
      <SessionProvider storage={null}>
        <Probe />
      </SessionProvider>,
    );
    await screen.findByText("state: signed-in");
    server.route({ "GET /api/v1/sign-in/session": BUSY });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await wait(0);

    unmount();
    await wait(0);

    expect(document.title).toBe("read");
  });

  it("stops reading again when it goes away", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(BEFORE_THE_END);
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    const user = userEvent.setup();
    const { unmount } = render(
      <SessionProvider storage={null}>
        <Probe />
      </SessionProvider>,
    );
    await screen.findByText("state: signed-in");
    server.route({ "GET /api/v1/sign-in/session": BUSY });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await act(() => Promise.resolve());
    const sent = server.sent.length;

    unmount();
    await wait(10 * SESSION_RETRY_FIRST_MS);

    expect(server.sent).toHaveLength(sent);
  });

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
