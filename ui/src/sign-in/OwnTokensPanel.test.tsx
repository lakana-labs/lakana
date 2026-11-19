import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TokenOut } from "../api/tokens";
import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { ADMIN_SESSION, type FakeRoute } from "../test/server";
import { signedIn } from "../test/session";
import { MemoryStorage } from "../test/storage";
import { fakeWebAuthn } from "../test/webauthn";

/**
 * A token of kim's.
 *
 * @param over - What differs from a live token named laptop.
 * @returns The token.
 */
function aToken(over: Partial<TokenOut> = {}): TokenOut {
  return {
    key_id: "k1",
    name: "laptop",
    owner: "kim",
    created_at: "2026-11-10T09:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    ...over,
  };
}

/**
 * Renders kim's account page, kim being no administrator.
 *
 * @param routes - The routes beyond the passkeys' and sessions'.
 * @returns The shell.
 */
function renderAccount(routes: Record<string, FakeRoute>): ReturnType<typeof renderShell> {
  return renderShell("/account", {
    me: { name: "kim", admin: false },
    session: signedIn({ session: { ...ADMIN_SESSION, name: "kim" } }),
    storage: new MemoryStorage(),
    routes: {
      "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
      "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  });
}

/**
 * Opens Create a token and types a name.
 *
 * @param shell - The rendered shell.
 * @param name - The token's name.
 * @returns The dialog.
 */
async function startCreating(
  shell: ReturnType<typeof renderShell>,
  name: string,
): Promise<HTMLElement> {
  await shell.user.click(await screen.findByRole("button", { name: "Create a token" }));
  const dialog = await screen.findByRole("dialog", { name: "Create a token" });
  await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), name);
  return dialog;
}

describe("OwnTokensPanel", () => {
  it("lists one's tokens, live first, each with its state", async () => {
    renderAccount({
      "GET /api/v1/principals/kim/tokens": {
        status: 200,
        body: [
          aToken({ key_id: "k2", name: "old", revoked_at: "2026-11-12T09:00:00Z" }),
          aToken({ key_id: "k3", name: "ci", expires_at: "2026-11-11T09:00:00Z" }),
          aToken({ last_used_at: "2026-11-15T09:00:00Z" }),
        ],
      },
    });

    const region = await screen.findByRole("region", { name: "Your API tokens" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual([
      "laptop",
      "old",
      "ci",
    ]);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1]?.textContent)).toEqual([
      "active",
      expect.stringMatching(/^revoked /),
      expect.stringMatching(/^expired /),
    ]);
    expect(
      within(region)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Revoke"]);
    expect(within(region).getByRole("button", { name: "Revoke laptop" })).toBeVisible();
    expect(within(rows[1] as HTMLElement).getAllByText("never")).toHaveLength(2);
  });

  it("the token's rights are the owner's", async () => {
    renderAccount({ "GET /api/v1/principals/kim/tokens": { status: 200, body: [] } });

    expect(await screen.findByText("You have no API token yet.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "A token signs in as you, with exactly your rights, from the command line and scripts.",
      ),
    ).toBeInTheDocument();
  });

  it("asks for the touch before creating", async () => {
    fakeWebAuthn();
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "POST /api/v1/principals/kim/tokens": {
        status: 201,
        body: { key_id: "k1", name: "laptop", token: "lak_x" },
      },
    });
    const dialog = await startCreating(shell, "laptop");

    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("dialog", { name: "Your new token laptop" })).toBeVisible();
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
      "POST /api/v1/principals/kim/tokens",
    ]);
    expect(shell.server.sent.at(-1)?.body).toEqual({ name: "laptop" });
  });

  it("shows the token once, never again", async () => {
    let made = false;
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": () => ({
        status: 200,
        body: made ? [aToken()] : [],
      }),
      "POST /api/v1/principals/kim/tokens": () => {
        made = true;
        return { status: 201, body: { key_id: "k1", name: "laptop", token: "lak_sentinel" } };
      },
    });
    const dialog = await startCreating(shell, "laptop");
    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));

    const shown = await screen.findByRole("dialog", { name: "Your new token laptop" });
    expect(within(shown).getByRole("button", { name: "Copy token" })).toBeVisible();
    expect(within(shown).getByText("lak_sentinel")).toBeVisible();
    const login = within(shown).getByText(`lakana login --url ${window.location.origin}`);
    expect(login.textContent).not.toContain("lak_sentinel");
    await shell.user.click(within(shown).getByRole("button", { name: "Done" }));

    const heading = screen.getByRole("heading", { level: 2, name: "API tokens" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(await screen.findByRole("button", { name: "Revoke laptop" })).toBeVisible();
    expect(document.body.innerHTML).not.toContain("lak_sentinel");
    for (const storage of [shell.storage, window.localStorage, window.sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index) ?? "";
        expect(`${key}=${storage.getItem(key) ?? ""}`).not.toContain("lak_sentinel");
      }
    }
  });

  it("a cancelled touch creates nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });
    const dialog = await startCreating(shell, "laptop");

    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("laptop");
    expect(
      shell.server.sent.some(
        (request) => request.path === "/api/v1/principals/kim/tokens" && request.method === "POST",
      ),
    ).toBe(false);
  });

  it("an account without a passkey hears the next step", async () => {
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      "GET /api/v1/sign-in/session": STALE_SESSION,
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: { type: "NoPasskeyError", detail: "Your account has no passkey at this address." },
      },
    });
    const dialog = await startCreating(shell, "laptop");

    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/no passkey yet/);
    expect(within(alert).getByRole("link")).toHaveAttribute("href", "/account");
  });

  it("cannot be closed while the token is being made, so it is shown", async () => {
    const request = held();
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      "POST /api/v1/principals/kim/tokens": request.route,
    });
    const dialog = await startCreating(shell, "laptop");
    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => {
      expect(shell.server.sent.some((sent) => sent.method === "POST")).toBe(true);
    });

    await shell.user.keyboard("{Escape}");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("dialog", { name: "Create a token" })).toBeInTheDocument();
    request.release({ status: 201, body: { key_id: "k1", name: "laptop", token: "lak_x" } });

    expect(await screen.findByRole("dialog", { name: "Your new token laptop" })).toBeVisible();
  });

  it("reads the list again after a refused create", async () => {
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [] },
      "POST /api/v1/principals/kim/tokens": { status: 500, body: { detail: "The server failed." } },
    });
    const dialog = await startCreating(shell, "laptop");
    await shell.user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The server failed.");

    await shell.user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(
        shell.server.sent.filter(
          (sent) => sent.method === "GET" && sent.path === "/api/v1/principals/kim/tokens",
        ),
      ).toHaveLength(2);
    });
  });

  it("Cancel and Escape make nothing and read nothing again", async () => {
    const shell = renderAccount({ "GET /api/v1/principals/kim/tokens": { status: 200, body: [] } });
    const dialog = await startCreating(shell, "laptop");
    await shell.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await startCreating(shell, "x");
    await shell.user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(
      shell.server.sent.filter((request) => request.path === "/api/v1/principals/kim/tokens"),
    ).toHaveLength(1);
  });

  it("revokes without a touch and keeps the focus", async () => {
    let revoked = false;
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": () => ({
        status: 200,
        body: [aToken(revoked ? { revoked_at: "2026-11-18T09:00:00Z" } : {})],
      }),
      "DELETE /api/v1/principals/kim/tokens/k1": () => {
        revoked = true;
        return { status: 204 };
      },
    });

    await shell.user.click(await screen.findByRole("button", { name: "Revoke laptop" }));
    const confirm = await screen.findByRole("dialog", { name: "Revoke this token?" });
    expect(confirm).toHaveTextContent("The token laptop of kim.");
    expect(confirm).toHaveTextContent("Scripts that use it stop at once.");
    expect(confirm).toHaveTextContent("Task tokens minted with it stop working too.");
    await shell.user.click(within(confirm).getByRole("button", { name: "Revoke the token" }));

    const heading = screen.getByRole("heading", { level: 2, name: "API tokens" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(await screen.findByText(/^revoked /)).toBeVisible();
    const index = shell.server.sent.findIndex((request) => request.method === "DELETE");
    expect(shell.server.sent[index]?.path).toBe("/api/v1/principals/kim/tokens/k1");
    expect(shell.server.sent[index - 1]?.path).not.toBe("/api/v1/sign-in/session");
  });

  it("closing the confirmation revokes nothing", async () => {
    const shell = renderAccount({
      "GET /api/v1/principals/kim/tokens": { status: 200, body: [aToken()] },
    });

    await shell.user.click(await screen.findByRole("button", { name: "Revoke laptop" }));
    await shell.user.click(
      within(await screen.findByRole("dialog", { name: "Revoke this token?" })).getByRole(
        "button",
        {
          name: "Cancel",
        },
      ),
    );

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(shell.server.sent.some((request) => request.method === "DELETE")).toBe(false);
  });
});
