import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { fakeWebAuthn } from "../test/webauthn";

const PASSKEY = {
  id: "p-1",
  name: "Laptop",
  created_at: "2026-11-01T10:00:00Z",
  last_used_at: null,
  backup_eligible: false,
  disabled_reason: null,
  usable_here: true,
};
const SESSION = {
  id: "s-1",
  via: "passkey",
  created_at: "2026-11-08T09:00:00Z",
  last_seen_at: "2026-11-08T10:00:00Z",
  absolute_expires_at: "2026-11-15T09:00:00Z",
  user_agent: "Firefox on Linux",
  peer: "192.0.2.1",
  current: false,
};

/**
 * The permissions route's answer for a person who is, or is not, an administrator.
 *
 * @param admin - Whether the person holds the ADMIN role on the instance.
 * @returns The fake route's answer.
 */
function permissions(admin: boolean): { status: number; body: unknown } {
  return {
    status: 200,
    body: {
      permissions: [{ via: admin ? [{ kind: "group", role: "ADMIN" }] : [] }],
    },
  };
}

describe("PersonPage", () => {
  it("shows another person's passkeys and sessions, without adding one for them", async () => {
    renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": permissions(false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [PASSKEY] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [SESSION] },
      },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "kim" })).toBeInTheDocument();
    expect(await screen.findByText("Laptop")).toBeInTheDocument();
    expect(await screen.findByText(/Firefox on Linux/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a passkey" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End all sessions" })).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Issue a recovery link" }),
    ).toBeInTheDocument();
  });

  it("shows the breadcrumb Access › People › the person", async () => {
    renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": permissions(false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    const nav = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: "Access" })).toHaveAttribute("href", "/access");
    expect(within(nav).getByRole("link", { name: "People" })).toHaveAttribute("href", "/access");
    expect(within(nav).getByText("kim")).toHaveAttribute("aria-current", "page");
  });

  it("shows the operator's command instead of a button for an administrator", async () => {
    renderShell("/access/people/ada", {
      routes: {
        "GET /api/v1/principals/ada/permissions": permissions(true),
        "GET /api/v1/principals/ada/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/ada/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByText("lakana recovery create ada")).toBeInTheDocument();
    expect(screen.getByText(/only the server operator can recover/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Issue a recovery link" })).not.toBeInTheDocument();
  });

  it("issues a recovery link for the person shown, with a passkey touch", async () => {
    fakeWebAuthn();
    let tries = 0;
    const { user } = renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": permissions(false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
        "POST /api/v1/principals/kim/recovery-link": () => {
          tries += 1;
          return tries === 1
            ? { status: 403, body: { type: "StepUpRequired", detail: "Touch." } }
            : {
                status: 201,
                body: {
                  id: "r-1",
                  name: "kim",
                  url: "https://lakana.example.com/sign-in#l=a&s=b",
                  expires_at: "2026-11-08T11:00:00Z",
                },
              };
        },
        "POST /api/v1/sign-in/step-up/options": {
          status: 200,
          body: { ceremony_id: "c", public_key: {} },
        },
        "POST /api/v1/sign-in/step-up": { status: 204 },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      await screen.findByText("https://lakana.example.com/sign-in#l=a&s=b"),
    ).toBeInTheDocument();
    expect(tries).toBe(2);
  });

  it("shows one's own page with the own controls and no recovery link", async () => {
    renderShell("/access/people/admin", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [PASSKEY] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByRole("button", { name: "Add a passkey" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Issue a recovery link" })).not.toBeInTheDocument();
  });

  it("offers the recovery link when the person's rights cannot be read", async () => {
    renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": { status: 500, body: { detail: "Broken." } },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    expect(
      await screen.findByRole("button", { name: "Issue a recovery link" }),
    ).toBeInTheDocument();
  });

  it("closes the recovery dialog with Cancel or Escape", async () => {
    const { user } = renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": permissions(false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Issue a recovery link" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers no recovery link for a service account, and says why", async () => {
    const { server } = renderShell("/access/people/loader", {
      routes: {
        "GET /api/v1/principals/loader": {
          status: 200,
          body: { name: "loader", type: "SERVICE", is_active: true },
        },
        "GET /api/v1/principals/loader/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/loader/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByText(/signs in with its API token/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Issue a recovery link" })).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.endsWith("/permissions"))).toBe(false);
  });
});
