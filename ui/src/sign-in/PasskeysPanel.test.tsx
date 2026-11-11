import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import { fakeWebAuthn } from "../test/webauthn";

const LAPTOP = {
  id: "p-1",
  name: "Laptop",
  created_at: "2026-11-01T10:00:00Z",
  last_used_at: "2026-11-08T10:00:00Z",
  backup_eligible: true,
  disabled_reason: null,
  usable_here: true,
};
const KEY = {
  id: "p-2",
  name: "",
  created_at: "2026-11-02T10:00:00Z",
  last_used_at: null,
  backup_eligible: false,
  disabled_reason: "possible_clone",
  usable_here: false,
};

describe("PasskeysPanel", () => {
  it("lists one's passkeys, each removed in place", async () => {
    renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [LAPTOP, KEY] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Passkeys and sessions" }),
    ).toBeInTheDocument();
    const laptop = (await screen.findByText("Laptop")).closest("tr") as HTMLElement;
    expect(within(laptop).getByText(/Synced/)).toBeInTheDocument();
    expect(within(laptop).getByRole("button", { name: "Remove Laptop" })).toBeVisible();
    expect(within(laptop).queryByRole("link")).not.toBeInTheDocument();
    const key = (await screen.findByText("Passkey")).closest("tr") as HTMLElement;
    expect(within(key).getByText(/Never used/)).toBeInTheDocument();
    expect(within(key).getByText(/Disabled: a copy of it was detected/)).toBeInTheDocument();
    expect(within(key).getByText(/Not usable at this address/)).toBeInTheDocument();
  });

  it("shows the pending sign on Add a passkey while the server is asked", async () => {
    fakeWebAuthn();
    const request = held();
    const { user } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
        "POST /api/v1/principals/admin/passkeys/options": request.route,
      },
    });
    await screen.findByText(/You have no passkey yet/);

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expectPending(screen.getByRole("button", { name: "Add a passkey" }));
    request.release({ status: 503, body: { type: "Busy", detail: "Busy." } });
    await screen.findByRole("alert");
  });

  it("adds a passkey and lists it", async () => {
    const webauthn = fakeWebAuthn();
    let mine: unknown[] = [];
    const { user } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": () => ({ status: 200, body: mine }),
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
        "POST /api/v1/principals/admin/passkeys/options": {
          status: 200,
          body: { ceremony_id: "c-1", public_key: {} },
        },
        "POST /api/v1/principals/admin/passkeys": () => {
          mine = [LAPTOP];
          return { status: 201, body: LAPTOP };
        },
      },
    });
    expect(await screen.findByText(/You have no passkey yet/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add a passkey" }));

    expect(await screen.findByText("Laptop")).toBeInTheDocument();
    expect(webauthn.create).toHaveBeenCalledTimes(1);
  });

  it("names the browser's refusal when adding a passkey fails", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.create.mockRejectedValue(new DOMException("no", "NotAllowedError"));
    const { user } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
        "POST /api/v1/principals/admin/passkeys/options": {
          status: 200,
          body: { ceremony_id: "c-1", public_key: {} },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Add a passkey" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/cancelled or took too long/);
  });

  it("removes one's passkey only after a confirmation, and focuses the panel after", async () => {
    let mine: unknown[] = [LAPTOP, KEY];
    const { user, server } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": () => ({ status: 200, body: mine }),
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
        "DELETE /api/v1/principals/admin/passkeys/p-1": () => {
          mine = [KEY];
          return { status: 204 };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Remove Laptop" }));
    const dialog = screen.getByRole("dialog", { name: "Remove Laptop?" });
    expect(dialog).toHaveTextContent("It no longer signs you in.");
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.click(within(dialog).getByRole("button", { name: "Remove the passkey" }));

    expect(await screen.findByRole("button", { name: "Remove Passkey" })).toBeInTheDocument();
    expect(screen.queryByText("Laptop")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Passkeys" })).toHaveFocus();
    });
    expect(server.sent.filter((request) => request.method === "DELETE")).toHaveLength(1);
  });

  it("sends nothing when the removal is cancelled, and gives focus back", async () => {
    const { user, server } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [LAPTOP] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
      },
    });

    const remove = await screen.findByRole("button", { name: "Remove Laptop" });
    await user.click(remove);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // React Aria hands focus back to the trigger in the next animation frame, not at once.
    await waitFor(() => {
      expect(remove).toHaveFocus();
    });
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);
  });

  it("shows the server's message when one's last passkey cannot be removed", async () => {
    const { user } = renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": { status: 200, body: [LAPTOP] },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
        "DELETE /api/v1/principals/admin/passkeys/p-1": {
          status: 409,
          body: {
            type: "ConflictError",
            detail: "This is your last working passkey. Add another one first.",
          },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Remove Laptop" }));
    await user.click(screen.getByRole("button", { name: "Remove the passkey" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This is your last working passkey. Add another one first.",
    );
    expect(screen.getByRole("dialog", { name: "Remove Laptop?" })).toBeVisible();
  });

  it("warns an administrator what removing another person's passkey does", async () => {
    let theirs: unknown[] = [LAPTOP];
    const { user, server } = renderShell("/access/people/kim", {
      routes: {
        "GET /api/v1/principals/kim": {
          status: 200,
          body: { name: "kim", type: "USER", is_active: true },
        },
        "GET /api/v1/principals/kim/permissions": { status: 200, body: { permissions: [] } },
        "GET /api/v1/principals/kim/passkeys": () => ({ status: 200, body: theirs }),
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
        "DELETE /api/v1/principals/kim/passkeys/p-1": () => {
          theirs = [];
          return { status: 204 };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Remove Laptop" }));
    const dialog = screen.getByRole("dialog", { name: "Remove Laptop from kim?" });
    expect(dialog).toHaveTextContent("kim is signed out everywhere");
    expect(dialog).toHaveTextContent("only the server operator can let them back in");
    await user.click(within(dialog).getByRole("button", { name: "Remove the passkey" }));

    expect(await screen.findByText("No passkey.")).toBeInTheDocument();
    expect(server.sent.find((request) => request.method === "DELETE")?.path).toBe(
      "/api/v1/principals/kim/passkeys/p-1",
    );
  });

  it("names the reason when the passkeys cannot be read", async () => {
    renderShell("/account", {
      routes: {
        "GET /api/v1/principals/admin/passkeys": {
          status: 500,
          body: { type: "HttpError", detail: "The server failed." },
        },
        "GET /api/v1/principals/admin/sessions": { status: 200, body: [] },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The server failed.");
  });
});
