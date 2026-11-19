import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { expectPending, held } from "../test/pending";
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
  user_agent: "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
  browser: "Firefox on Linux",
  peer: "192.0.2.1",
  current: false,
};

/**
 * The principal route's answer for a person who holds or has held the ADMIN role, or has not.
 *
 * @param name - The person's name.
 * @param heldAdmin - Whether they hold or have ever held ADMIN.
 * @returns The fake route's answer.
 */
function principal(name: string, heldAdmin: boolean): { status: number; body: unknown } {
  return {
    status: 200,
    body: { name, type: "USER", is_active: true, picture: null, held_admin: heldAdmin },
  };
}

/** A recovery link for kim, as the route that issues it answers. */
const ISSUED_LINK = {
  id: "r-1",
  name: "kim",
  url: "https://lakana.example.com/sign-in#l=a&s=b",
  expires_at: "2026-11-08T11:00:00Z",
};

describe("PersonPage", () => {
  it("shows another person's passkeys and sessions, without adding one for them", async () => {
    renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": principal("kim", false),
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

  it("marks People current on a person's page and leads back by the breadcrumb", async () => {
    renderShell("/settings/access/people/maria", {
      routes: {
        "GET /api/v1/principals/maria": principal("maria", false),
        "GET /api/v1/principals/maria/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/maria/sessions": { status: 200, body: [] },
      },
    });

    const nav = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => [link.textContent, link.getAttribute("href")]),
    ).toEqual([
      ["Settings", "/settings"],
      ["Access", "/settings/access"],
      ["People", "/settings/access/people"],
    ]);
    expect(within(nav).getByText("maria")).toHaveAttribute("aria-current", "page");
    const places = screen.getByRole("navigation", { name: "Settings places" });
    expect(within(places).getByRole("link", { name: "People" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows the operator's command instead of a button for a former administrator", async () => {
    // The ADMIN role was taken away, so the rights read none; the durable mark still counts.
    const { server } = renderShell("/settings/access/people/ada", {
      routes: {
        "GET /api/v1/principals/ada": principal("ada", true),
        "GET /api/v1/principals/ada/permissions": {
          status: 200,
          body: { permissions: [{ via: [] }] },
        },
        "GET /api/v1/principals/ada/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/ada/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByText("lakana recovery create ada")).toBeInTheDocument();
    expect(
      screen.getByText(
        "ada is or was an administrator, so only the server operator can recover this account " +
          "(taking the role away does not change that): run this where the server runs, with " +
          "the bootstrap token.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Issue a recovery link" })).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.endsWith("/permissions"))).toBe(false);
  });

  it("issues a recovery link for the person shown, with the passkey touch first", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": principal("kim", false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/principals/kim/recovery-link": { status: 201, body: ISSUED_LINK },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    const from = server.sent.length;
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText(ISSUED_LINK.url)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(
      server.sent
        .slice(from)
        .filter((request) => request.method !== "GET" || request.path === "/api/v1/sign-in/session")
        .map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/principals/kim/recovery-link",
    ]);
  });

  it("falls back to the touch and one retry when the server still answers StepUpRequired", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const { user } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": principal("kim", false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
        // The touch looks fresh, but ends before the request reaches the server.
        "GET /api/v1/sign-in/session": freshSession(),
        ...STEP_UP_ROUTES,
        "POST /api/v1/principals/kim/recovery-link": () => {
          tries += 1;
          return tries === 1
            ? { status: 403, body: { type: "StepUpRequired", detail: "Touch." } }
            : { status: 201, body: ISSUED_LINK };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText(ISSUED_LINK.url)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(tries).toBe(2);
  });

  it("shows the pending sign on Continue while the recovery link is made", async () => {
    const request = held();
    const { user } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": principal("kim", false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/principals/kim/recovery-link": request.route,
      },
    });
    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));

    await user.click(screen.getByRole("button", { name: "Continue" }));

    const dialog = screen.getByRole("dialog");
    expectPending(within(dialog).getByRole("button", { name: /^Continue/ }));
    expect(within(dialog).getByRole("button", { name: "Cancel" })).not.toHaveAttribute(
      "data-pending",
    );
    request.release({ status: 403, body: { type: "PermissionDenied", detail: "No." } });
    await within(dialog).findByRole("alert");
  });

  it("shows the pending sign on the confirming button while a picture is removed", async () => {
    const request = held();
    const { user } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": {
          status: 200,
          body: { name: "kim", type: "USER", is_active: true, picture: "e1" },
        },
        "GET /api/v1/principals/kim/picture": {
          status: 200,
          blob: new Blob(["webp"], { type: "image/webp" }),
        },
        "DELETE /api/v1/principals/kim/picture": request.route,
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });
    await user.click(await screen.findByRole("button", { name: "Remove picture" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove kim's picture?" });

    await user.click(within(dialog).getByRole("button", { name: "Remove picture" }));

    expectPending(within(dialog).getByRole("button", { name: /^Remove picture/ }));
    request.release({ status: 204 });
    await vi.waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("shows one's own page with the own controls and no recovery link", async () => {
    renderShell("/settings/access/people/admin", {
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
    renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": { status: 500, body: { detail: "Broken." } },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    expect(
      await screen.findByRole("button", { name: "Issue a recovery link" }),
    ).toBeInTheDocument();
  });

  it("closes the recovery dialog with Cancel or Escape", async () => {
    const { user } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": principal("kim", false),
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
    const { server } = renderShell("/settings/access/people/loader", {
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

  it("lets an administrator remove another person's picture", async () => {
    const { user, server } = renderShell("/settings/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": {
          status: 200,
          body: { name: "kim", type: "USER", is_active: true, picture: "e1" },
        },
        "GET /api/v1/principals/kim/picture": {
          status: 200,
          blob: new Blob(["webp"], { type: "image/webp" }),
        },
        "DELETE /api/v1/principals/kim/picture": { status: 204 },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Remove picture" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove picture" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove kim's picture?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove picture" }));

    await vi.waitFor(() => {
      expect(screen.queryByRole("button", { name: "Remove picture" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { level: 1, name: "kim" })).toHaveFocus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
  });

  it("offers no removal for a person without a picture, nor on one's own page", async () => {
    renderShell("/settings/access/people/admin", {
      me: { picture: "e1" },
      routes: {
        "GET /api/v1/principals/admin/picture": {
          status: 200,
          blob: new Blob(["webp"], { type: "image/webp" }),
        },
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "admin" })).toBeInTheDocument();
    await vi.waitFor(() => {
      expect(screen.getByRole("main").querySelector("img")?.getAttribute("src")).toMatch(/^blob:/);
    });
    expect(screen.queryByRole("button", { name: "Remove picture" })).not.toBeInTheDocument();
  });

  it("marks hidden characters of the person's name and a passkey's name, in the dialogs too", async () => {
    const base = "/api/v1/principals/k\u202Eim";
    const { user } = renderShell("/settings/access/people/k%E2%80%AEim", {
      routes: {
        [`GET ${base}`]: principal("k\u202Eim", false),
        [`GET ${base}/passkeys`]: { status: 200, body: [{ ...PASSKEY, name: "Lap\u2066top" }] },
        [`GET ${base}/sessions`]: { status: 200, body: [SESSION] },
      },
    });

    const remove = await screen.findByRole("button", { name: /LapU\+2066top/u });
    expect(document.body.textContent).not.toMatch(/[\u202E\u2066]/u);
    await user.click(remove);
    expect(await screen.findByRole("dialog")).toHaveTextContent("kU+202Eim");
    expect(document.body.textContent).not.toMatch(/[\u202E\u2066]/u);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "End all sessions" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("kU+202Eim");
    expect(document.body.textContent).not.toMatch(/[\u202E\u2066]/u);
    await user.keyboard("{Escape}");
    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("kU+202Eim");
    expect(document.body.textContent).not.toMatch(/[\u202E\u2066]/u);
  });

  it("marks a hidden character of the person's name in the issued link's sentence", async () => {
    const base = "/api/v1/principals/k\u202Eim";
    const { user } = renderShell("/settings/access/people/k%E2%80%AEim", {
      routes: {
        [`GET ${base}`]: principal("k\u202Eim", false),
        [`GET ${base}/passkeys`]: { status: 200, body: [] },
        [`GET ${base}/sessions`]: { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        [`POST ${base}/recovery-link`]: {
          status: 201,
          body: {
            id: "r-1",
            name: "k\u202Eim",
            url: "https://lakana.example.com/sign-in#l=a&s=b",
            expires_at: "2026-11-08T11:00:00Z",
          },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const sentence = await screen.findByText(/^It works once, for 1 hour/u);
    expect(sentence).toHaveTextContent("send it to kU+202Eim yourself.");
    expect(document.body.textContent).not.toMatch(/\u202E/u);
  });
});
