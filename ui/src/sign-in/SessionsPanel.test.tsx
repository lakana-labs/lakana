import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { signedIn } from "../test/session";

const FIREFOX = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0";
const HERE = {
  id: "s-1",
  via: "passkey",
  created_at: "2026-11-08T09:00:00Z",
  last_seen_at: "2026-11-08T10:00:00Z",
  absolute_expires_at: "2026-11-15T09:00:00Z",
  user_agent: FIREFOX,
  browser: "Firefox 158 on Ubuntu",
  peer: "192.0.2.1",
  current: true,
};
const THERE = {
  ...HERE,
  id: "s-2",
  user_agent: "",
  browser: "",
  peer: "198.51.100.7",
  current: false,
};

describe("SessionsPanel", () => {
  it("lists one's sessions and marks this browser", async () => {
    renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [HERE, THERE] },
      },
    });

    const named = await screen.findByText("Firefox 158 on Ubuntu, from 192.0.2.1");
    expect(named).toHaveAttribute("title", FIREFOX);
    expect(screen.queryByText(/Mozilla/)).not.toBeInTheDocument();
    expect(screen.getByText("This browser")).toBeInTheDocument();
    expect(screen.getByText(/Unknown browser/)).toBeInTheDocument();
    expect(screen.getByText(/198\.51\.100\.7/)).toBeInTheDocument();
  });

  it("signs out everywhere through the session, so this browser signs out too", async () => {
    const session = signedIn();
    const { user } = renderShell("/account", {
      session,
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [HERE] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));

    expect(session.signOutEverywhere).toHaveBeenCalledTimes(1);
  });

  it("ends another person's sessions and lists none afterwards", async () => {
    let open: unknown[] = [THERE];
    const { user, server } = renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": { status: 200, body: { permissions: [] } },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": () => ({ status: 200, body: open }),
        "DELETE /api/v1/principals/kim/sessions": () => {
          open = [];
          return { status: 204 };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "End all sessions" }));
    const dialog = screen.getByRole("dialog", { name: "End all sessions of kim?" });
    expect(dialog).toHaveTextContent("kim is signed out in every browser");
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);
    await user.click(screen.getByRole("button", { name: "End the sessions" }));

    expect(await screen.findByText("No open session.")).toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Sessions" })).toHaveFocus();
    });
  });

  it("ends nothing when the administrator cancels", async () => {
    const { user, server } = renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim/permissions": { status: 200, body: { permissions: [] } },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [THERE] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "End all sessions" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);
  });

  it("names the server's reason when ending sessions fails", async () => {
    const session = signedIn({
      signOutEverywhere: () =>
        Promise.reject(new DOMException("The browser refused.", "SecurityError")),
    });
    const { user } = renderShell("/account", {
      session,
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [HERE] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Passkeys work only at the address/);
  });
});
