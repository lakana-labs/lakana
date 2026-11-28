import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROVIDER_TRIP_KEY } from "../api/oidc";
import { STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import { ADMIN_SESSION, type FakeRoute } from "../test/server";
import { signedIn } from "../test/session";
import { MemoryStorage } from "../test/storage";
import { fakeWebAuthn } from "../test/webauthn";

const OKTA = { status: 200, body: { enabled: true, display_name: "Okta" } };
const LAPTOP = {
  id: "p-1",
  name: "Laptop",
  created_at: "2026-11-01T10:00:00Z",
  last_used_at: "2026-11-08T10:00:00Z",
  backup_eligible: true,
  disabled_reason: null,
  usable_here: true,
};

/**
 * Renders kim's account page.
 *
 * @param linked - Whether kim signs in through the provider.
 * @param routes - The routes beyond the panels' reads.
 * @returns The shell.
 */
function renderAccount(
  linked: boolean,
  routes: Record<string, FakeRoute>,
): ReturnType<typeof renderShell> {
  return renderShell("/account", {
    me: { name: "kim", admin: false },
    session: signedIn({ session: { ...ADMIN_SESSION, name: "kim", provider_linked: linked } }),
    storage: new MemoryStorage(),
    routes: {
      "GET /api/v1/principals/kim/passkeys": { status: 200, body: [LAPTOP] },
      "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      ...routes,
    },
  });
}

/**
 * The panel of a heading.
 *
 * @param name - The panel's heading.
 * @returns The panel.
 */
async function panel(name: string): Promise<HTMLElement> {
  return screen.findByRole("region", { name });
}

describe("ProviderSection", () => {
  afterEach(() => {
    sessionStorage.clear();
  });

  it("says whether the person is linked and until when tokens work", async () => {
    renderAccount(true, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/whoami": {
        status: 200,
        body: { name: "kim", token_window_ends_at: "2026-12-25T09:00:00Z" },
      },
    });

    const section = await panel("Sign-in");
    expect(within(section).getByText("You sign in through Okta.")).toBeInTheDocument();
    expect(
      await within(section).findByText(
        /^Your API tokens work until .*2026.*; signing in through Okta renews this\.$/,
      ),
    ).toBeInTheDocument();
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
  });

  it("a linked person whose window cannot be read sees the section without it", async () => {
    renderAccount(true, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/whoami": { status: 503, body: { detail: "Busy.", type: "Busy" } },
    });

    const section = await panel("Sign-in");
    expect(within(section).getByText("You sign in through Okta.")).toBeInTheDocument();
    expect(within(section).queryByText(/^Your API tokens work until/)).not.toBeInTheDocument();
  });

  it("a linked person's passkeys are listed without Add", async () => {
    renderAccount(true, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/whoami": { status: 200, body: { name: "kim", token_window_ends_at: null } },
    });

    const passkeys = await panel("Passkeys");
    expect(await within(passkeys).findByText("Laptop")).toBeInTheDocument();
    expect(
      within(passkeys).getByText("Your passkeys are kept but no longer sign you in."),
    ).toBeInTheDocument();
    // Only the list's Name header, which sorts: no Add and no Remove.
    expect(
      within(passkeys)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Name"]);
  });

  it("the tokens panel shows the window for a linked person only", async () => {
    renderAccount(true, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/whoami": {
        status: 200,
        body: { name: "kim", token_window_ends_at: "2026-12-25T09:00:00Z" },
      },
    });
    const tokens = await panel("API tokens");
    expect(await within(tokens).findByText(/^Your API tokens work until /)).toBeInTheDocument();
  });

  it("an unlinked person's tokens panel has no window, and nothing is read for it", async () => {
    const shell = renderAccount(false, { "GET /api/v1/sign-in/oidc": OKTA });

    const section = await panel("Sign-in");
    expect(
      within(section).getByText(
        "You sign in with a passkey. You can sign in through Okta instead.",
      ),
    ).toBeInTheDocument();
    const tokens = await panel("API tokens");
    expect(within(tokens).queryByText(/^Your API tokens work until /)).not.toBeInTheDocument();
    expect(
      within(await panel("Passkeys")).getByRole("button", { name: "Add a passkey" }),
    ).toBeVisible();
    expect(shell.server.sent.filter((request) => request.path === "/api/v1/whoami")).toEqual([]);
  });

  it("with OIDC off the profile has no Sign-in section", async () => {
    renderAccount(false, {
      "GET /api/v1/sign-in/oidc": { status: 200, body: { enabled: false, display_name: null } },
    });

    expect(await panel("Passkeys")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Sign-in" })).not.toBeInTheDocument();
    });
    expect(document.body).not.toHaveTextContent(/provider/i);
  });

  it("the link dialog says passkeys stop signing in before Continue", async () => {
    const shell = renderAccount(false, { "GET /api/v1/sign-in/oidc": OKTA });

    await shell.user.click(await screen.findByRole("button", { name: "Use Okta to sign in" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Sign in through Okta from now on?",
    });
    expect(
      within(dialog).getByText(
        "From then on you sign in only through Okta. Your passkeys stay stored but no longer sign you in.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeVisible();
    expect(
      shell.server.sent.filter((request) => request.path.startsWith("/api/v1/sign-in/oidc/")),
    ).toEqual([]);
  });

  it("Continue asks for the passkey touch, then goes to the provider", async () => {
    fakeWebAuthn();
    const shell = renderAccount(false, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "POST /api/v1/sign-in/oidc/link/start": {
        status: 200,
        body: { authorization_url: "https://id.example.org/link" },
      },
    });
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/account", search: "", hash: "", assign });

    await shell.user.click(await screen.findByRole("button", { name: "Use Okta to sign in" }));
    const dialog = await screen.findByRole("dialog");
    await shell.user.click(within(dialog).getByRole("button", { name: "Continue" }));

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith("https://id.example.org/link");
    });
    const start = shell.server.sent.findIndex(
      (request) => request.path === "/api/v1/sign-in/session",
    );
    expect(
      shell.server.sent
        .slice(start, start + 4)
        .map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/sign-in/oidc/link/start",
    ]);
    expect(shell.server.sent.at(-1)?.body).toEqual({ return_to: "/account" });
    expect(sessionStorage.getItem(PROVIDER_TRIP_KEY)).toBe(
      JSON.stringify({ purpose: "link", back: "/account" }),
    );
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a refused link stays in the dialog with its cause", async () => {
    const shell = renderAccount(false, {
      "GET /api/v1/sign-in/oidc": OKTA,
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
      },
      "POST /api/v1/sign-in/oidc/link/start": {
        status: 403,
        body: {
          detail: "You are the last administrator who signs in with a passkey.",
          type: "OidcSignInRefused",
          reason: "last_unlinked_admin",
        },
      },
    });

    await shell.user.click(await screen.findByRole("button", { name: "Use Okta to sign in" }));
    const dialog = await screen.findByRole("dialog");
    await shell.user.click(within(dialog).getByRole("button", { name: "Continue" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "You are the last administrator who signs in with a passkey.",
    );
  });

  it("Escape returns the focus to Use provider to sign in", async () => {
    const shell = renderAccount(false, { "GET /api/v1/sign-in/oidc": OKTA });
    const open = await screen.findByRole("button", { name: "Use Okta to sign in" });

    await shell.user.click(open);
    await screen.findByRole("dialog");
    await shell.user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(open).toHaveFocus();
    });
  });
});
