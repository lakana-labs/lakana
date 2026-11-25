import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SessionContext, type SessionValue } from "./SessionProvider";
import { SignInPage } from "./SignInPage";

import { expectPending, held } from "../test/pending";
import { fakeServer } from "../test/server";
import { signedIn } from "../test/session";
import { fakeWebAuthn } from "../test/webauthn";

const OKTA = { status: 200, body: { enabled: true, display_name: "Okta" } };
const STARTED = {
  status: 200,
  body: { authorization_url: "https://id.example.org/authorize?state=s" },
};
const OPTIONS = { status: 200, body: { ceremony_id: "c-1", public_key: { challenge: "abc" } } };

/**
 * Renders the sign-in page for a signed-out browser.
 *
 * @param overrides - Fields of the session value.
 * @param from - The page the person was going to, if any.
 * @returns The session value and a user-event session.
 */
function renderPage(
  overrides: Partial<SessionValue> = {},
  from?: string,
): {
  session: SessionValue;
  user: ReturnType<typeof userEvent.setup>;
} {
  const session = signedIn({ state: "signed-out", session: undefined, ...overrides });
  const user = userEvent.setup();
  render(
    <SessionContext value={session}>
      <SignInPage {...(from === undefined ? {} : { from })} />
    </SessionContext>,
  );
  return { session, user };
}

describe("SignInPage", () => {
  it("offers the passkey and focuses its heading", () => {
    renderPage();

    const heading = screen.getByRole("heading", { level: 1, name: "Sign in to Lakana" });
    expect(heading).toHaveFocus();
    expect(document.title).toBe("Sign in to Lakana · Lakana");
    expect(screen.getByRole("button", { name: "Sign in with a passkey" })).toBeInTheDocument();
    expect(
      screen.getByText("No passkey on this device? Ask an administrator for a recovery link."),
    ).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("says why the last session ended", () => {
    renderPage({ reason: "Your session reached its maximum length and ended. Sign in again." });

    expect(
      screen.getByText("Your session reached its maximum length and ended. Sign in again."),
    ).toBeInTheDocument();
  });

  it("signs in with a passkey and reloads the session", async () => {
    fakeWebAuthn();
    fakeServer({
      "POST /api/v1/sign-in/passkey/options": OPTIONS,
      "POST /api/v1/sign-in/passkey": {
        status: 200,
        body: { name: "admin", needs_passkey: false, offer_passkey: false, csrf_token: "c" },
      },
    });
    const { session, user } = renderPage();

    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));

    expect(session.refresh).toHaveBeenCalledTimes(1);
  });

  it("names a passkey the server refused", async () => {
    fakeWebAuthn();
    fakeServer({
      "POST /api/v1/sign-in/passkey/options": OPTIONS,
      "POST /api/v1/sign-in/passkey": {
        status: 401,
        body: { detail: "Not signed in.", type: "AuthenticationError" },
      },
    });
    const { session, user } = renderPage();

    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/^This passkey did not sign you in/);
    expect(session.refresh).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign in with a passkey" })).toBeEnabled();
  });

  it("shows the pending sign on the button while the passkey is asked for", async () => {
    fakeWebAuthn();
    const request = held();
    fakeServer({ "POST /api/v1/sign-in/passkey/options": request.route });
    const { user } = renderPage();

    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));

    expectPending(screen.getByRole("button", { name: /^Sign in with a passkey/ }));
    request.release({ status: 503, body: { type: "Busy", detail: "Busy." } });
    await screen.findByRole("alert");
  });

  it("names a cancelled passkey request and shows its wait in words", async () => {
    const webauthn = fakeWebAuthn();
    let cancel: (error: unknown) => void = () => undefined;
    webauthn.get.mockReturnValueOnce(
      new Promise((_, reject) => {
        cancel = reject;
      }),
    );
    fakeServer({ "POST /api/v1/sign-in/passkey/options": OPTIONS });
    const { user } = renderPage();

    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Waiting for your passkey…");
    cancel(new DOMException("x", "NotAllowedError"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^The passkey request was cancelled/,
    );
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("with OIDC off the sign-in page is unchanged", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/oidc": { status: 200, body: { enabled: false, display_name: null } },
    });
    renderPage();

    await waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });
    expect(screen.getByRole("button", { name: "Sign in with a passkey" })).toHaveClass(
      "button-primary",
    );
    expect(screen.queryByRole("button", { name: "Use a passkey" })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/provider|Okta/i);
    expect(server.sent.map((request) => request.path)).toEqual(["/api/v1/sign-in/oidc"]);
  });

  it("offers the provider first and a passkey below", async () => {
    fakeServer({ "GET /api/v1/sign-in/oidc": OKTA });
    renderPage();

    const provider = await screen.findByRole("button", { name: "Sign in with Okta" });
    const passkey = screen.getByRole("button", { name: "Use a passkey" });
    expect(provider).toHaveClass("button-primary");
    expect(passkey).not.toHaveClass("button-primary");
    expect(provider.compareDocumentPosition(passkey) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(screen.getByText("For people who don't sign in through Okta.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toHaveFocus();
  });

  it("starting keeps where the person was going", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/oidc": OKTA,
      "POST /api/v1/sign-in/oidc/start": STARTED,
    });
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });
    const { user } = renderPage({}, "/sign-in/cli?port=49152&state=s-1");

    await user.click(await screen.findByRole("button", { name: "Sign in with Okta" }));

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith("https://id.example.org/authorize?state=s");
    });
    expect(server.sent[1]?.body).toEqual({ return_to: "/sign-in/cli?port=49152&state=s-1" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("names a provider that cannot be reached, on the page", async () => {
    fakeServer({
      "GET /api/v1/sign-in/oidc": OKTA,
      "POST /api/v1/sign-in/oidc/start": {
        status: 403,
        body: {
          detail: "The provider could not be reached.",
          type: "OidcSignInRefused",
          reason: "provider_unreachable",
        },
      },
    });
    const { user } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Sign in with Okta" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The provider could not be reached.",
    );
  });

  it("signs in with a passkey below the provider", async () => {
    fakeWebAuthn();
    fakeServer({
      "GET /api/v1/sign-in/oidc": OKTA,
      "POST /api/v1/sign-in/passkey/options": OPTIONS,
      "POST /api/v1/sign-in/passkey": {
        status: 200,
        body: { name: "admin", needs_passkey: false, offer_passkey: false, csrf_token: "c" },
      },
    });
    const { session, user } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Use a passkey" }));

    expect(session.refresh).toHaveBeenCalledTimes(1);
  });

  it("marks hidden characters in the provider name", async () => {
    fakeServer({
      "GET /api/v1/sign-in/oidc": {
        status: 200,
        body: { enabled: true, display_name: "Ok\u202Eta" },
      },
    });
    renderPage();

    const button = await screen.findByRole("button", { name: /^Sign in with Ok/ });
    expect(button).toHaveTextContent("Sign in with OkU+202Eta");
    expect(document.body.textContent).not.toContain("\u202E");
  });

  it("a failed provider read shows the passkey page", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/oidc": { status: 503, body: { detail: "Busy.", type: "Busy" } },
    });
    renderPage();

    await waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });
    expect(screen.getByRole("button", { name: "Sign in with a passkey" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
