import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";

const OPEN = {
  id: "r-1",
  name: "kim",
  created_at: "2026-11-08T10:00:00Z",
  expires_at: "2026-11-08T11:00:00Z",
};
const ISSUED = {
  id: "r-2",
  name: "sam",
  url: "https://lakana.example.com/sign-in#l=pub&s=secret",
  expires_at: "2026-11-08T11:00:00Z",
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

describe("RecoveryLinksPage", () => {
  it("lists the open links; each row opens its person", async () => {
    const { user, router } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [OPEN] },
        "GET /api/v1/principals/kim": principal("kim", false),
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    const row = (await screen.findByRole("link", { name: "kim" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("›")).toBeInTheDocument();
    await user.click(within(row).getByRole("link", { name: "kim" }));

    expect(router.state.location.pathname).toBe("/access/people/kim");
    expect(await screen.findByRole("heading", { level: 1, name: "kim" })).toBeInTheDocument();
  });

  it("says how to issue a link when none is open", async () => {
    renderShell("/access/recovery-links", {
      routes: { "GET /api/v1/recovery-links": { status: 200, body: [] } },
    });

    expect(await screen.findByText(/No recovery link is open/)).toBeInTheDocument();
  });

  it("issues a link for a person who is not an administrator and shows it once", async () => {
    let open: unknown[] = [];
    const { user } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": () => ({ status: 200, body: open }),
        "GET /api/v1/principals/sam": principal("sam", false),
        "POST /api/v1/principals/sam/recovery-link": () => {
          open = [{ ...OPEN, id: "r-2", name: "sam" }];
          return { status: 201, body: ISSUED };
        },
      },
    });
    await screen.findByText(/No recovery link is open/);

    await user.click(screen.getByRole("button", { name: "Issue a recovery link" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "sam");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const shown = await screen.findByRole("dialog", { name: "Send this link to sam" });
    expect(within(shown).getByText(ISSUED.url)).toBeInTheDocument();
    expect(within(shown).getByText(/works once, for 1 hour/)).toBeInTheDocument();
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("link", { name: "sam" })).toBeInTheDocument();
  });

  it("shows the operator's command for an administrator instead of issuing a link", async () => {
    const { user, server } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/ada": principal("ada", true),
      },
    });
    await screen.findByText(/No recovery link is open/);

    await user.click(screen.getByRole("button", { name: "Issue a recovery link" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "ada");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const dialog = await screen.findByRole("dialog", { name: "ada is or was an administrator" });
    expect(within(dialog).getByText("lakana recovery create ada")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Continue" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Done" })).toHaveFocus();
    expect(server.sent.some((request) => request.method === "POST")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("names the server's reason for an unknown person", async () => {
    const { user } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/nobody": {
          status: 404,
          body: { type: "NotFoundError", detail: "No principal named 'nobody'." },
        },
      },
    });
    await screen.findByText(/No recovery link is open/);

    await user.click(screen.getByRole("button", { name: "Issue a recovery link" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "nobody");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("No principal named 'nobody'.");
  });

  it("shows the pending sign on Withdraw while the link is withdrawn", async () => {
    const request = held();
    const { user } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [OPEN] },
        "DELETE /api/v1/recovery-links/r-1": request.route,
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Withdraw the recovery link of kim" }),
    );

    const button = screen.getByRole("button", { name: /^Withdraw the recovery link of kim/ });
    expectPending(button);
    request.release({ status: 204 });
    await waitFor(() => {
      expect(button.querySelector(".pending-sign")).toBeNull();
    });
  });

  it("shows the pending sign only on the Withdraw that was pressed", async () => {
    const request = held();
    const { user } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": {
          status: 200,
          body: [OPEN, { ...OPEN, id: "r-3", name: "sam" }],
        },
        "DELETE /api/v1/recovery-links/r-1": request.route,
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Withdraw the recovery link of kim" }),
    );

    expectPending(screen.getByRole("button", { name: /^Withdraw the recovery link of kim/ }));
    const other = screen.getByRole("button", { name: "Withdraw the recovery link of sam" });
    expect(other.querySelector(".pending-sign")).toBeNull();
    expect(other).toBeDisabled();
    request.release({ status: 204 });
  });

  it("withdraws a link in place", async () => {
    let open: unknown[] = [OPEN];
    const { user } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": () => ({ status: 200, body: open }),
        "DELETE /api/v1/recovery-links/r-1": () => {
          open = [];
          return { status: 204 };
        },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Withdraw the recovery link of kim" }),
    );

    expect(await screen.findByText(/No recovery link is open/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Access" })).toHaveFocus();
    });
  });

  it("marks a hidden character of the person's name", async () => {
    renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [{ ...OPEN, name: "k\u202Eim" }] },
      },
    });

    const link = await screen.findByRole("link", { name: "kU+202Eim" });
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByRole("button", { name: /kU\+202Eim/u })).toBeInTheDocument();
    expect(row.textContent).not.toMatch(/[\u202E\u2066]/u);
  });
});
