import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LinkLanding } from "./LinkLanding";
import { SessionContext, type SessionValue } from "./SessionProvider";

import { expectPending, held } from "../test/pending";
import { fakeServer } from "../test/server";
import { signedIn } from "../test/session";

/**
 * Renders the landing page of a link.
 *
 * @param link - The link's two halves.
 * @param link.linkId - The public id.
 * @param link.secret - The secret.
 * @param session - The session value; signed out by default.
 * @returns The done callback and a user-event session.
 */
function renderLanding(
  link: { linkId: string; secret: string },
  session: SessionValue = signedIn({ state: "signed-out", session: undefined }),
): { onDone: ReturnType<typeof vi.fn>; user: ReturnType<typeof userEvent.setup> } {
  const onDone = vi.fn();
  const user = userEvent.setup();
  render(
    <SessionContext value={session}>
      <LinkLanding link={link} onDone={onDone} />
    </SessionContext>,
  );
  return { onDone, user };
}

describe("LinkLanding", () => {
  it("names an incomplete link without asking the server", async () => {
    const server = fakeServer();
    const { onDone, user } = renderLanding({ linkId: "", secret: "sec-1" });

    expect(
      screen.getByRole("heading", { level: 1, name: "This link does not work" }),
    ).toHaveFocus();
    expect(server.sent).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Go to sign-in" }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("names the person by their display name", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "invite", name: "kim", display_name: "Kim Lee", state: "open" },
      },
    });
    renderLanding({ linkId: "pub-1", secret: "sec-1" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Join Lakana as Kim Lee" }),
    ).toHaveFocus();
    expect(screen.getByText(/^This invitation creates your account/)).toBeInTheDocument();
  });

  it("names an unreachable server while checking", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("offline"))),
    );
    renderLanding({ linkId: "pub-1", secret: "sec-1" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/^Lakana could not be reached/);
  });

  it("names a link that became unusable before it was confirmed", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "recovery", name: "kim", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 410,
        body: { detail: "Withdrawn.", type: "LinkUnusableError", state: "withdrawn" },
      },
    });
    const { user } = renderLanding({ linkId: "pub-1", secret: "sec-1" });

    await user.click(await screen.findByRole("button", { name: "Continue" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "This link was withdrawn" }),
    ).toBeInTheDocument();
  });

  it("passes any other refusal on with the server's words, and may be tried again", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "recovery", name: "kim", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 429,
        body: { detail: "Too many attempts; wait a minute.", type: "TooManyAttempts" },
      },
    });
    const { user } = renderLanding({ linkId: "pub-1", secret: "sec-1" });

    await user.click(await screen.findByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts; wait a minute.");
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("offers Home to a signed-in person whose link cannot be used", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 410,
        body: { detail: "Used.", type: "LinkUnusableError", state: "used" },
      },
    });
    const { onDone, user } = renderLanding({ linkId: "pub-1", secret: "sec-1" }, signedIn());

    await user.click(await screen.findByRole("button", { name: "Go to Home" }));

    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it.each([{ state: "strange" }, {}])(
    "treats a missing or unknown state %j as an unknown link",
    async (extra) => {
      fakeServer({
        "POST /api/v1/sign-in/links/describe": {
          status: 410,
          body: { detail: "?", type: "LinkUnusableError", ...extra },
        },
      });
      renderLanding({ linkId: "pub-1", secret: "sec-1" });

      expect(
        await screen.findByRole("heading", { level: 1, name: "This link does not work" }),
      ).toBeInTheDocument();
    },
  );

  it.each([
    [200, { kind: "local", name: "admin", display_name: null, state: "open" }],
    [410, { detail: "Used.", type: "LinkUnusableError", state: "used" }],
  ])("never acts on an answer (%s) that arrives after the page was left", async (status, body) => {
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    const server = fakeServer({
      "POST /api/v1/sign-in/links/describe": () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const onDone = vi.fn();
    const { unmount } = render(
      <SessionContext value={signedIn({ state: "signed-out", session: undefined })}>
        <LinkLanding link={{ linkId: "pub-1", secret: "sec-1" }} onDone={onDone} />
      </SessionContext>,
    );
    await screen.findByText("Checking your sign-in link…");

    unmount();
    answer({ status, body });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(server.sent.map((request) => request.path)).toEqual(["/api/v1/sign-in/links/describe"]);
    expect(onDone).not.toHaveBeenCalled();
  });

  it.each([
    "This browser is already signed in, so the local sign-in link was not used. Sign out first.",
    "That name was taken meanwhile; ask for a new invite.",
    "The web interface is off on this server; ask the operator to turn it on.",
  ])("shows the server's own reason for a 409: %s", async (detail) => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "invite", name: "kim", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": {
        status: 409,
        body: { detail, type: "ConflictError" },
      },
    });
    const { user } = renderLanding({ linkId: "pub-1", secret: "sec-1" });

    await user.click(await screen.findByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  it("offers to check the link again after the server could not be reached", async () => {
    let calls = 0;
    fakeServer({
      "POST /api/v1/sign-in/links/describe": () => {
        calls += 1;
        return calls === 1
          ? { status: 503, body: { detail: "Busy; try again.", type: "Busy" } }
          : {
              status: 200,
              body: { kind: "recovery", name: "kim", display_name: null, state: "open" },
            };
      },
    });
    const { user } = renderLanding({ linkId: "pub-1", secret: "sec-1" });

    expect(await screen.findByRole("alert")).toHaveTextContent("Busy; try again.");
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in as kim" }),
    ).toBeInTheDocument();
    expect(calls).toBe(2);
  });

  it("shows the pending sign on Continue while the link is used", async () => {
    const request = held();
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "recovery", name: "kim", display_name: null, state: "open" },
      },
      "POST /api/v1/sign-in/links/redeem": request.route,
    });
    const { user } = renderLanding({ linkId: "pub-1", secret: "sec-1" });

    await user.click(await screen.findByRole("button", { name: "Continue" }));

    expectPending(screen.getByRole("button", { name: "Continue" }));
    request.release({ status: 503, body: { detail: "Busy.", type: "Busy" } });
    await screen.findByRole("alert");
  });

  it("says that continuing ends the session this browser has", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "recovery", name: "kim", display_name: null, state: "open" },
      },
    });
    renderLanding({ linkId: "pub-1", secret: "sec-1" }, signedIn());

    expect(
      await screen.findByText(
        "This browser is signed in as admin; continuing signs that session out.",
      ),
    ).toBeInTheDocument();
  });
});
