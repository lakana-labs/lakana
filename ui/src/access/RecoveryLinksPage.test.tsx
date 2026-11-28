import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import { columnTexts, sortBy, sortState } from "../test/sorting";
import { fakeWebAuthn } from "../test/webauthn";

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

/**
 * Opens Issue a recovery link, types the name and continues.
 *
 * @param user - The user-event session.
 * @param name - The name to type.
 */
async function issueFor(user: ReturnType<typeof renderShell>["user"], name: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Issue a recovery link" }));
  await user.type(screen.getByRole("textbox", { name: "Name" }), name);
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("RecoveryLinksPage", () => {
  it("sorts the open recovery links by name and by expiry", async () => {
    const { user } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": {
          status: 200,
          body: [
            { ...OPEN, id: "r-1", name: "kim", expires_at: "2026-11-08T11:00:00Z" },
            { ...OPEN, id: "r-2", name: "ana", expires_at: "2026-11-08T12:00:00Z" },
          ],
        },
      },
    });
    const region = await screen.findByRole("region", { name: "Open recovery links" });

    await sortBy(user, region, "Name");
    expect(columnTexts(region)).toEqual(["ana", "kim"]);
    await sortBy(user, region, "Name");
    expect(sortState(region, "Name")).toBe("descending");
    expect(columnTexts(region)).toEqual(["kim", "ana"]);
    await sortBy(user, region, "Expires");
    expect(columnTexts(region)).toEqual(["kim", "ana"]);
  });

  it("keeps names whole in their own region", async () => {
    const long = "k".repeat(300);
    renderShell("/settings/sign-in/recovery-links", {
      routes: { "GET /api/v1/recovery-links": { status: 200, body: [{ ...OPEN, name: long }] } },
    });

    const region = await screen.findByRole("region", { name: "Open recovery links" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("list-scroll", "recovery-scroll");
    const names = region.querySelectorAll(".row-name");
    expect(names).toHaveLength(1);
    expect(names[0]?.tagName).toBe("A");
    expect(within(region).getByRole("link", { name: long })).toHaveAttribute(
      "href",
      `/settings/access/people/${long}`,
    );
    expect(within(region).getByText("›")).toBeInTheDocument();
  });

  it("asks for the touch before issuing", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/sam": principal("sam", false),
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/principals/sam/recovery-link": { status: 201, body: ISSUED },
      },
    });
    await screen.findByText(/No recovery link is open/);
    const from = server.sent.length;

    await issueFor(user, "sam");

    expect(await screen.findByRole("dialog", { name: "Send this link to sam" })).toBeVisible();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(server.sent.slice(from).map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/principals/sam",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/principals/sam/recovery-link",
    ]);
  });

  it("a cancelled touch issues nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { user, server } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/sam": principal("sam", false),
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/principals/sam/recovery-link": { status: 201, body: ISSUED },
      },
    });
    await screen.findByText(/No recovery link is open/);

    await issueFor(user, "sam");

    expect(await screen.findByRole("alert")).toHaveTextContent(/cancelled or took too long/);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("sam");
    expect(server.sent.some((request) => request.path.endsWith("/recovery-link"))).toBe(false);
  });

  it("returns the focus to Issue a recovery link after Done", async () => {
    const { user } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/sam": principal("sam", false),
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/principals/sam/recovery-link": { status: 201, body: ISSUED },
      },
    });
    await screen.findByText(/No recovery link is open/);

    await issueFor(user, "sam");
    const shown = await screen.findByRole("dialog", { name: "Send this link to sam" });
    await user.click(within(shown).getByRole("button", { name: "Done" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Issue a recovery link" })).toHaveFocus();
    });
    expect(screen.queryByText(ISSUED.url)).not.toBeInTheDocument();
  });

  it("returns the focus to Issue a recovery link after the operator's command", async () => {
    const { user } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [] },
        "GET /api/v1/principals/ada": principal("ada", true),
      },
    });
    await screen.findByText(/No recovery link is open/);

    await issueFor(user, "ada");
    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Issue a recovery link" })).toHaveFocus();
    });
  });

  it("lists the open links; each row opens its person", async () => {
    const { user, router } = renderShell("/settings/sign-in/recovery-links", {
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

    expect(router.state.location.pathname).toBe("/settings/access/people/kim");
    expect(await screen.findByRole("heading", { level: 1, name: "kim" })).toBeInTheDocument();
  });

  it("says how to issue a link when none is open", async () => {
    renderShell("/settings/sign-in/recovery-links", {
      routes: { "GET /api/v1/recovery-links": { status: 200, body: [] } },
    });

    expect(await screen.findByText(/No recovery link is open/)).toBeInTheDocument();
  });

  it("issues a link for a person who is not an administrator and shows it once", async () => {
    let open: unknown[] = [];
    const { user } = renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": () => ({ status: 200, body: open }),
        "GET /api/v1/principals/sam": principal("sam", false),
        "GET /api/v1/sign-in/session": freshSession(),
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
    const { user, server } = renderShell("/settings/sign-in/recovery-links", {
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
    const { user } = renderShell("/settings/sign-in/recovery-links", {
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
    const { user } = renderShell("/settings/sign-in/recovery-links", {
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
    const { user } = renderShell("/settings/sign-in/recovery-links", {
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
    const { user } = renderShell("/settings/sign-in/recovery-links", {
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
      expect(screen.getByRole("heading", { level: 1, name: "Recovery links" })).toHaveFocus();
    });
  });

  it("marks a hidden character of the person's name", async () => {
    renderShell("/settings/sign-in/recovery-links", {
      routes: {
        "GET /api/v1/recovery-links": { status: 200, body: [{ ...OPEN, name: "k\u202Eim" }] },
      },
    });

    const link = await screen.findByRole("link", { name: "kU+202Eim" });
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByRole("button", { name: /kU\+202Eim/u })).toBeInTheDocument();
    expect(row.textContent).not.toMatch(/[\u202E\u2066]/u);
  });

  it("reads the open recovery links again on Refresh", async () => {
    const { server, user } = renderShell("/settings/sign-in/recovery-links", {
      routes: { "GET /api/v1/recovery-links": { status: 200, body: [OPEN] } },
    });
    expect(await screen.findByText("1 open recovery link")).toBeInTheDocument();
    const reads = (): number =>
      server.sent.filter(
        (request) => request.method === "GET" && request.path.startsWith("/api/v1/recovery-links"),
      ).length;
    await waitFor(() => {
      expect(reads()).toBeGreaterThan(0);
    });
    const before = reads();

    await user.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
  });
});
