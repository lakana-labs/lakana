import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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

describe("InvitesPage", () => {
  it("lists the open invites; each row is withdrawn in place, not opened", async () => {
    renderShell("/access/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [OPEN] } },
    });

    const row = (await screen.findByText("kim")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Kim Lee")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Withdraw the invite of kim" })).toBeVisible();
    expect(within(row).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Access" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Invites" })).toHaveAttribute("aria-current", "page");
  });

  it("says how to invite someone when no invite is open", async () => {
    renderShell("/access/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [] } },
    });

    expect(await screen.findByText(/No invite is open/)).toBeInTheDocument();
    expect(screen.getByText("lakana invite create <name>")).toBeInTheDocument();
  });

  it("shows the new link once, with the advice to confirm the person first", async () => {
    let open: unknown[] = [];
    const { user, server } = renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": () => ({ status: 200, body: open }),
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
    const { user, server } = renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
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

  it("names the server's reason when the invite is refused, and keeps the dialog", async () => {
    const { user } = renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
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
    const { user } = renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [] },
        "POST /api/v1/invites": request.route,
      },
    });
    await screen.findByText(/No invite is open/);
    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    const dialog = screen.getByRole("dialog", { name: "Invite someone" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "sam");

    await user.click(within(dialog).getByRole("button", { name: "Create the invite" }));

    expectPending(within(dialog).getByRole("button", { name: "Create the invite" }));
    expect(within(dialog).getByRole("button", { name: "Cancel" })).not.toHaveAttribute(
      "data-pending",
    );
    request.release({ status: 201, body: ISSUED });
    await screen.findByRole("dialog", { name: "Send this link to sam" });
  });

  it("shows the pending sign on Withdraw while the invite is withdrawn", async () => {
    const request = held();
    const { user } = renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": { status: 200, body: [OPEN] },
        "DELETE /api/v1/invites/i-1": request.route,
      },
    });

    await user.click(await screen.findByRole("button", { name: "Withdraw the invite of kim" }));

    const button = screen.getByRole("button", { name: "Withdraw the invite of kim" });
    expectPending(button);
    request.release({ status: 204 });
    await waitFor(() => {
      expect(button.querySelector(".pending-sign")).toBeNull();
    });
  });

  it("withdraws an invite and removes its row", async () => {
    let open: unknown[] = [OPEN];
    const { user, server } = renderShell("/access/invites", {
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
      expect(screen.getByRole("heading", { level: 1, name: "Access" })).toHaveFocus();
    });
  });

  it("names the server's reason when withdrawing fails", async () => {
    const { user } = renderShell("/access/invites", {
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
    renderShell("/access/invites", {
      routes: {
        "GET /api/v1/invites": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });
});
