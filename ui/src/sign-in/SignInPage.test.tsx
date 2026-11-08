import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SessionContext, type SessionValue } from "./SessionProvider";
import { SignInPage } from "./SignInPage";

import { fakeServer } from "../test/server";
import { signedIn } from "../test/session";
import { fakeWebAuthn } from "../test/webauthn";

const OPTIONS = { status: 200, body: { ceremony_id: "c-1", public_key: { challenge: "abc" } } };

/**
 * Renders the sign-in page for a signed-out browser.
 *
 * @param overrides - Fields of the session value.
 * @returns The session value and a user-event session.
 */
function renderPage(overrides: Partial<SessionValue> = {}): {
  session: SessionValue;
  user: ReturnType<typeof userEvent.setup>;
} {
  const session = signedIn({ state: "signed-out", session: undefined, ...overrides });
  const user = userEvent.setup();
  render(
    <SessionContext value={session}>
      <SignInPage />
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
});
