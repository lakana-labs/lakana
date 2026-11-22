import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import { fakeWebAuthn } from "../test/webauthn";

const OPEN = {
  id: "i-1",
  name: "kim",
  display_name: "Kim Lee",
  created_at: "2026-11-08T10:00:00Z",
  expires_at: "2026-11-15T10:00:00Z",
};
const ISSUED = {
  id: "i-2",
  name: "sam",
  display_name: null,
  url: "https://lakana.example.com/sign-in#l=pub&s=secret",
  expires_at: "2026-11-15T10:00:00Z",
};

/**
 * Opens Invite someone, types the name and creates the invite.
 *
 * @param user - The user-event session.
 * @param name - The name to type.
 */
async function createInviteFor(
  user: ReturnType<typeof renderShell>["user"],
  name: string,
): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Invite someone" }));
  const dialog = screen.getByRole("dialog", { name: "Invite someone" });
  await user.type(within(dialog).getByRole("textbox", { name: "Name" }), name);
  await user.click(within(dialog).getByRole("button", { name: "Create the invite" }));
}

describe("InvitesPage", () => {
  it("keeps names whole in their own region", async () => {
    const long = "k".repeat(300);
    renderShell("/settings/sign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [{ ...OPEN, name: long }] } },
    });

    const region = await screen.findByRole("region", { name: "Open invites" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("list-scroll", "invites-scroll");
    expect(within(region).getByRole("table")).toHaveClass("list", "list-static");
    const names = region.querySelectorAll(".row-name");
    expect(names).toHaveLength(1);
    expect(names[0]?.tagName).toBe("SPAN");
    expect(names[0]).toHaveTextContent(long);
    expect(within(region).queryByRole("link")).not.toBeInTheDocument();
    expect(within(region).getByText("Kim Lee")).toHaveClass("second");
  });

  it("asks for the touch before creating the invite", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/invites": { status: 201, body: { ...ISSUED, name: "kim" } },
      },
    });
    await screen.findByText(/No invite is open/);
    const from = server.sent.length;

    await createInviteFor(user, "kim");

    const shown = await screen.findByRole("dialog", { name: "Send this link to kim" });
    expect(within(shown).getByText(ISSUED.url)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    const writes = server.sent
      .slice(from)
      .filter((request) => request.path !== "/api/v1/invites" || request.method !== "GET");
    expect(writes.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/invites",
    ]);
    expect(writes.at(-1)?.body).toEqual({ name: "kim", display_name: null });
  });

  it("a cancelled touch creates nothing and keeps the name", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/invites": { status: 201, body: ISSUED },
      },
    });
    await screen.findByText(/No invite is open/);

    await createInviteFor(user, "kim");

    const dialog = screen.getByRole("dialog", { name: "Invite someone" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The passkey request was cancelled or took too long.",
    );
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("kim");
    expect(
      server.sent.some(
        (request) => request.method === "POST" && request.path === "/api/v1/invites",
      ),
    ).toBe(false);
  });

  it.each(["Done", "Escape"])("returns the focus to Invite someone after %s", async (how) => {
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": { status: 201, body: ISSUED },
      },
    });
    await screen.findByText(/No invite is open/);

    await createInviteFor(user, "sam");
    const shown = await screen.findByRole("dialog", { name: "Send this link to sam" });
    if (how === "Done") {
      await user.click(within(shown).getByRole("button", { name: "Done" }));
    } else {
      await user.keyboard("{Escape}");
    }

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Invite someone" })).toHaveFocus();
    });
    expect(screen.queryByText(ISSUED.url)).not.toBeInTheDocument();
  });

  it("returns the focus to Invite someone after Cancel", async () => {
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [] } },
    });
    await user.click(await screen.findByRole("button", { name: "Invite someone" }));

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Invite someone" })).toHaveFocus();
    });
  });

  it("withdraws without a touch, and the focus goes to the heading when the last invite goes", async () => {
    let open: unknown[] = [OPEN];
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": () => ({ status: 200, body: open }),
        "GET /api/v1/sign-in/session": STALE_SESSION,
        "DELETE /api/v1/invites/i-1": () => {
          open = [];
          return { status: 204 };
        },
      },
    });
    const withdraw = await screen.findByRole("button", { name: "Withdraw the invite of kim" });
    const from = server.sent.length;

    await user.click(withdraw);

    expect(await screen.findByText(/No invite is open/)).toBeInTheDocument();
    expect(server.sent[from]).toMatchObject({ method: "DELETE", path: "/api/v1/invites/i-1" });
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Invites" })).toHaveFocus();
    });
  });

  it("lists the open invites; each row is withdrawn in place, not opened", async () => {
    renderShell("/settings/sign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [OPEN] } },
    });

    const row = (await screen.findByText("kim")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Kim Lee")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Withdraw the invite of kim" })).toBeVisible();
    expect(within(row).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Invites" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Invites" })).toHaveAttribute("aria-current", "page");
  });

  it("says how to invite someone when no invite is open", async () => {
    renderShell("/settings/sign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [] } },
    });

    expect(await screen.findByText(/No invite is open/)).toBeInTheDocument();
    expect(screen.getByText("lakana invite create <name>")).toBeInTheDocument();
  });

  it("shows the new link once, with the advice to confirm the person first", async () => {
    let open: unknown[] = [];
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": () => ({ status: 200, body: open }),
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": () => {
          open = [{ ...OPEN, id: "i-2", name: "sam", display_name: null }];
          return { status: 201, body: ISSUED };
        },
      },
    });
    await screen.findByText(/No invite is open/);

    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    const dialog = screen.getByRole("dialog", { name: "Invite someone" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "sam");
    await user.click(within(dialog).getByRole("button", { name: "Create the invite" }));

    const shown = await screen.findByRole("dialog", { name: "Send this link to sam" });
    expect(within(shown).getByText(ISSUED.url)).toBeInTheDocument();
    expect(within(shown).getByRole("button", { name: "Copy link" })).toBeVisible();
    expect(within(shown).getByText(/This is the only time Lakana shows it/)).toBeInTheDocument();
    expect(
      within(shown).getByText(/confirm outside Lakana that the person who signed up is sam/),
    ).toBeInTheDocument();
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      name: "sam",
      display_name: null,
    });

    await user.click(within(shown).getByRole("button", { name: "Done" }));

    expect(screen.queryByText(ISSUED.url)).not.toBeInTheDocument();
    expect(await screen.findByText("sam")).toBeInTheDocument();
  });

  it("asks for a passkey touch when the server needs one, then sends once more", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": () => {
          tries += 1;
          return tries === 1
            ? {
                status: 403,
                body: { type: "StepUpRequired", detail: "Confirm with your passkey." },
              }
            : { status: 201, body: ISSUED };
        },
        "POST /api/v1/sign-in/step-up/options": {
          status: 200,
          body: { ceremony_id: "c-1", public_key: {} },
        },
        "POST /api/v1/sign-in/step-up": { status: 204 },
      },
    });
    await screen.findByText(/No invite is open/);

    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "sam");
    await user.click(screen.getByRole("button", { name: "Create the invite" }));

    expect(await screen.findByText(ISSUED.url)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(
      server.sent.filter((request) => request.method === "POST").map((request) => request.path),
    ).toEqual([
      "/api/v1/invites",
      "/api/v1/sign-in/step-up/options",
      "/api/v1/sign-in/step-up",
      "/api/v1/invites",
    ]);
  });

  it("says to add a passkey first when the account has none, and links to where", async () => {
    // No browser support for passkeys at all: the server's answer comes first, so the person
    // learns that the account has no passkey, not that the browser cannot use one.
    vi.stubGlobal("PublicKeyCredential", undefined);
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": {
          status: 403,
          body: { type: "StepUpRequired", detail: "Confirm with your passkey." },
        },
        "POST /api/v1/sign-in/step-up/options": {
          status: 409,
          body: { type: "NoPasskeyError", detail: "Your account has no passkey at this address." },
        },
      },
    });
    await screen.findByText(/No invite is open/);

    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "sam");
    await user.click(screen.getByRole("button", { name: "Create the invite" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Your account has no passkey yet, and this action needs a passkey touch. Add one first " +
        "under Passkeys and sessions, in the menu under your name, then try again.",
    );
    expect(within(alert).getByRole("link", { name: "Add a passkey" })).toHaveAttribute(
      "href",
      "/account",
    );
    expect(
      server.sent.filter((request) => request.method === "POST").map((request) => request.path),
    ).toEqual(["/api/v1/invites", "/api/v1/sign-in/step-up/options"]);
  });

  it("names the server's reason when the invite is refused, and keeps the dialog", async () => {
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": {
          status: 409,
          body: { type: "ConflictError", detail: "'Kim' is taken. Choose another name." },
        },
      },
    });
    await screen.findByText(/No invite is open/);

    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Kim");
    await user.type(screen.getByRole("textbox", { name: /Display name/ }), "Kim Lee");
    await user.click(screen.getByRole("button", { name: "Create the invite" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "'Kim' is taken. Choose another name.",
    );
    expect(screen.getByRole("dialog", { name: "Invite someone" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the pending sign on Create the invite while the invite is made", async () => {
    const request = held();
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "GET /api/v1/sign-in/session": freshSession(),
        "POST /api/v1/invites": request.route,
      },
    });
    await screen.findByText(/No invite is open/);
    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    const dialog = screen.getByRole("dialog", { name: "Invite someone" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "sam");

    await user.click(within(dialog).getByRole("button", { name: "Create the invite" }));

    expectPending(within(dialog).getByRole("button", { name: /^Create the invite/ }));
    expect(within(dialog).getByRole("button", { name: "Cancel" })).not.toHaveAttribute(
      "data-pending",
    );
    request.release({ status: 201, body: ISSUED });
    await screen.findByRole("dialog", { name: "Send this link to sam" });
  });

  it("shows the pending sign on Withdraw while the invite is withdrawn", async () => {
    const request = held();
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [OPEN] },
        "DELETE /api/v1/invites/i-1": request.route,
      },
    });

    await user.click(await screen.findByRole("button", { name: "Withdraw the invite of kim" }));

    const button = screen.getByRole("button", { name: /^Withdraw the invite of kim/ });
    expectPending(button);
    request.release({ status: 204 });
    await waitFor(() => {
      expect(button.querySelector(".pending-sign")).toBeNull();
    });
  });

  it("shows the pending sign only on the Withdraw that was pressed", async () => {
    const request = held();
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": {
          status: 200,
          body: [OPEN, { ...OPEN, id: "i-3", name: "sam", display_name: null }],
        },
        "DELETE /api/v1/invites/i-1": request.route,
      },
    });

    await user.click(await screen.findByRole("button", { name: "Withdraw the invite of kim" }));

    expectPending(screen.getByRole("button", { name: /^Withdraw the invite of kim/ }));
    const other = screen.getByRole("button", { name: "Withdraw the invite of sam" });
    expect(other.querySelector(".pending-sign")).toBeNull();
    expect(other).toBeDisabled();
    request.release({ status: 204 });
  });

  it("withdraws an invite and removes its row", async () => {
    let open: unknown[] = [OPEN];
    const { user, server } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": () => ({ status: 200, body: open }),
        "DELETE /api/v1/invites/i-1": () => {
          open = [];
          return { status: 204 };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Withdraw the invite of kim" }));

    expect(await screen.findByText(/No invite is open/)).toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Invites" })).toHaveFocus();
    });
  });

  it("names the server's reason when withdrawing fails", async () => {
    const { user } = renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [OPEN] },
        "DELETE /api/v1/invites/i-1": {
          status: 409,
          body: { type: "ConflictError", detail: "The invite was used already." },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Withdraw the invite of kim" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The invite was used already.");
  });

  it("names the reason when the list cannot be read", async () => {
    renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });

  it("marks hidden characters of an invite's name and display name", async () => {
    renderShell("/settings/sign-in/invites", {
      routes: {
        "GET /api/v1/invites": {
          status: 200,
          body: [{ ...OPEN, name: "k\u202Eim", display_name: "Kim\u2066 Lee" }],
        },
      },
    });

    const button = await screen.findByRole("button", { name: "Withdraw the invite of kU+202Eim" });
    const row = button.closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("kU+202Eim");
    expect(row).toHaveTextContent("KimU+2066 Lee");
    expect(row.textContent).not.toMatch(/[\u202E\u2066]/u);
  });

  it("reads the open invites again on Refresh", async () => {
    const { server, user } = renderShell("/settings/sign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [OPEN] } },
    });
    expect(await screen.findByText("1 open invite")).toBeInTheDocument();
    const reads = (): number =>
      server.sent.filter(
        (request) => request.method === "GET" && request.path.startsWith("/api/v1/invites"),
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
