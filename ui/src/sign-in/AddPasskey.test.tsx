import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AddPasskey } from "./AddPasskey";
import { SessionContext, type SessionValue } from "./SessionProvider";

import { expectPending, held } from "../test/pending";
import { ADMIN_SESSION, fakeServer } from "../test/server";
import { signedIn } from "../test/session";
import { fakeWebAuthn } from "../test/webauthn";

const OPTIONS = { status: 200, body: { ceremony_id: "c-1", public_key: { challenge: "abc" } } };

/**
 * Renders the passkey step.
 *
 * @param required - Whether the step is required.
 * @returns The session value and a user-event session.
 */
function renderStep(required: boolean): {
  session: SessionValue;
  user: ReturnType<typeof userEvent.setup>;
} {
  const account = { ...ADMIN_SESSION, display_name: "Kim Lee", needs_passkey: required };
  const session = signedIn({ session: account });
  const user = userEvent.setup();
  render(
    <SessionContext value={session}>
      <AddPasskey required={required} account={account} />
    </SessionContext>,
  );
  return { session, user };
}

describe("AddPasskey", () => {
  it("is required: no way past it but adding one or signing out", async () => {
    const { session, user } = renderStep(true);

    expect(screen.getByRole("heading", { level: 1, name: "Add a passkey" })).toHaveFocus();
    expect(screen.getByText("Signed in as Kim Lee.")).toBeInTheDocument();
    expect(screen.getByText(/^You signed in with a link that works once/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Not now" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(session.signOut).toHaveBeenCalledTimes(1);
  });

  it("is an offer that may be skipped", async () => {
    const { session, user } = renderStep(false);

    expect(screen.getByText(/^Sign in faster next time/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not now" }));

    expect(session.skipPasskey).toHaveBeenCalledTimes(1);
  });

  it("adds a passkey to the person's own account and reloads the session", async () => {
    const webauthn = fakeWebAuthn();
    const server = fakeServer({
      "POST /api/v1/principals/admin/passkeys/options": OPTIONS,
      "POST /api/v1/principals/admin/passkeys": { status: 201, body: { id: "p-1" } },
    });
    const { session, user } = renderStep(true);

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expect(webauthn.create).toHaveBeenCalledTimes(1);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/principals/admin/passkeys/options",
      "/api/v1/principals/admin/passkeys",
    ]);
    expect(session.refresh).toHaveBeenCalledTimes(1);
  });

  it("shows the pending sign on Add a passkey while the server is asked", async () => {
    fakeWebAuthn();
    const request = held();
    fakeServer({ "POST /api/v1/principals/admin/passkeys/options": request.route });
    const { user } = renderStep(true);

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expectPending(screen.getByRole("button", { name: "Add a passkey" }));
    request.release({ status: 503, body: { type: "Busy", detail: "Busy." } });
    await screen.findByRole("alert");
  });

  it("names a browser that cannot add a passkey", async () => {
    fakeWebAuthn();
    const { session, user } = renderStep(true);
    Reflect.deleteProperty(globalThis, "PublicKeyCredential");

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/^This browser cannot use passkeys/);
    expect(session.refresh).not.toHaveBeenCalled();
  });
});
