import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { ADMIN_SESSION } from "../test/server";
import { signedIn } from "../test/session";

/**
 * The routes of one's own page, as an administrator or not.
 *
 * @param admin - Whether the signed-in person holds the ADMIN role.
 * @returns The fake routes.
 */
function ownRoutes(admin: boolean): Record<string, { status: number; body: unknown }> {
  return {
    "GET /api/v1/principals/admin/permissions": {
      status: 200,
      body: { permissions: [{ via: admin ? [{ kind: "grant", role: "ADMIN" }] : [] }] },
    },
    "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
    "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
  };
}

describe("OwnRecoveryNote", () => {
  it("tells an administrator on Passkeys and sessions that only the operator can let them back in", async () => {
    renderShell("/account", { routes: ownRoutes(true) });

    expect(
      await screen.findByText(/If you lose every passkey, only the server operator/),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana recovery create admin")).toBeInTheDocument();
  });

  it("tells a linked administrator that the operator's recovery unlinks them", async () => {
    renderShell("/account", {
      session: signedIn({ session: { ...ADMIN_SESSION, provider_linked: true } }),
      routes: {
        ...ownRoutes(true),
        "GET /api/v1/sign-in/oidc": { status: 200, body: { enabled: true, display_name: "Okta" } },
        "GET /api/v1/whoami": { status: 200, body: { token_window_ends_at: null } },
      },
    });

    expect(
      await screen.findByText(
        /^If you cannot sign in through the identity provider .* only the server operator can let you back in.* unlinks your account from the provider/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/If you lose every passkey/)).not.toBeInTheDocument();
    expect(screen.getByText("lakana recovery create admin")).toBeInTheDocument();
  });

  it("says the same on an administrator's own page in Access", async () => {
    renderShell("/settings/access/people/admin", { routes: ownRoutes(true) });

    expect(await screen.findByText("lakana recovery create admin")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Issue a recovery link" })).not.toBeInTheDocument();
  });

  it("says nothing to a person who is not an administrator", async () => {
    const { server } = renderShell("/account", { routes: ownRoutes(false) });

    await waitFor(() => {
      expect(server.sent.map((request) => request.path)).toContain(
        "/api/v1/principals/admin/permissions",
      );
    });
    await act(() => Promise.resolve());
    expect(screen.queryByText(/lakana recovery create/)).not.toBeInTheDocument();
  });

  it("says nothing when the role cannot be read", async () => {
    const { server } = renderShell("/account", {
      routes: {
        ...ownRoutes(true),
        "GET /api/v1/principals/admin/permissions": {
          status: 500,
          body: { detail: "The catalog database broke." },
        },
      },
    });

    await waitFor(() => {
      expect(server.sent.map((request) => request.path)).toContain(
        "/api/v1/principals/admin/permissions",
      );
    });
    await act(() => Promise.resolve());
    expect(screen.queryByText(/lakana recovery create/)).not.toBeInTheDocument();
  });
});
