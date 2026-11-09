import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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
 * The permissions route's answer for a person who is, or is not, an administrator.
 *
 * @param admin - Whether the person holds the ADMIN role on the instance.
 * @returns The fake route's answer.
 */
function permissions(admin: boolean): { status: number; body: unknown } {
  return {
    status: 200,
    body: {
      principal: "x",
      active: true,
      scope: { kind: "INSTANCE", environment: null, namespace: null, table: null },
      permissions: [
        {
          action: "MANAGE_PRINCIPAL",
          allowed: admin,
          via: admin ? [{ kind: "role", role: "ADMIN" }] : [],
          reason: null,
        },
      ],
      note: null,
    },
  };
}

describe("RecoveryLinksPage", () => {
  it("lists the open links; each row opens its person", async () => {
    const { user, router } = renderShell("/access/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [OPEN] },
        "GET /api/v1/principals/kim/permissions": permissions(false),
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
        "GET /api/v1/principals/sam/permissions": permissions(false),
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
        "GET /api/v1/principals/ada/permissions": permissions(true),
      },
    });
    await screen.findByText(/No recovery link is open/);

    await user.click(screen.getByRole("button", { name: "Issue a recovery link" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "ada");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const dialog = await screen.findByRole("dialog", { name: "ada is an administrator" });
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
        "GET /api/v1/principals/nobody/permissions": {
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
});
