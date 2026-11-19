import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

/** dev, live and unprotected. */
const DEV: EnvironmentOut = {
  name: "dev",
  storage_root: "file:///srv/lakana/dev",
  storage_secret_ref: null,
  is_protected: false,
  properties: {},
  retention_days: null,
  deleted_at: null,
  purge_after: null,
};

/**
 * dev as the server answers its deletion.
 *
 * @param purge - Whether the files go at the next clean-up.
 * @returns The environment.
 */
const gone = (purge: boolean): EnvironmentOut => ({
  ...DEV,
  deleted_at: "2026-11-19T09:00:00Z",
  purge_after: purge ? "2026-11-19T09:00:00Z" : "2026-11-26T09:00:00Z",
});

/**
 * dev's page for an administrator, with Delete environment open.
 *
 * @param routes - More routes.
 * @returns The shell and the dialog.
 */
async function openDelete(
  routes: Record<string, FakeRoute> = {},
): Promise<ReturnType<typeof renderShell> & { readonly dialog: HTMLElement }> {
  const shell = renderShell("/settings/environments/dev", {
    routes: {
      "GET /api/v1/environments/dev": { status: 200, body: DEV },
      "GET /api/v1/environments/dev/usage": { status: 500, body: { detail: "down" } },
      "GET /api/v1/environments": { status: 200, body: [] },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  });
  await shell.user.click(await screen.findByRole("button", { name: "Delete environment dev" }));
  return {
    ...shell,
    dialog: await screen.findByRole("dialog", { name: "Delete environment dev?" }),
  };
}

describe("Delete environment", () => {
  it("a protected environment offers no delete", async () => {
    renderShell("/settings/environments/dev", {
      routes: {
        "GET /api/v1/environments/dev": { status: 200, body: { ...DEV, is_protected: true } },
      },
    });

    expect(
      await screen.findByText("Protected environments cannot be deleted. Unprotect it first."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete environment dev" }),
    ).not.toBeInTheDocument();
  });

  it("deletes and keeps the files without a touch", async () => {
    const shell = await openDelete({
      "DELETE /api/v1/environments/dev": { status: 200, body: gone(false) },
    });
    const { dialog, user, server } = shell;
    expect(dialog).toHaveTextContent("its task tokens stop working");
    expect(
      within(dialog).getByRole("radio", { name: /Keep the files for the undo period/ }),
    ).toBeChecked();
    const submit = within(dialog).getByRole("button", { name: "Delete environment" });
    expect(submit).toBeDisabled();

    await user.type(within(dialog).getByRole("textbox", { name: "Type dev to delete it" }), "dev");
    await user.click(submit);

    expect(await screen.findByText(/Deleted dev\. It can be restored until/)).toBeInTheDocument();
    expect(shell.router.state.location.pathname).toBe("/settings/environments");
    expect(screen.getByRole("heading", { level: 1, name: "Environments" })).toHaveFocus();
    const del = server.sent.find((request) => request.method === "DELETE");
    expect(del?.path).toBe("/api/v1/environments/dev");
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("purges after the touch", async () => {
    fakeWebAuthn();
    const shell = await openDelete({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "DELETE /api/v1/environments/dev": { status: 200, body: gone(true) },
    });
    const { dialog, user, server } = shell;

    await user.click(within(dialog).getByRole("radio", { name: /Delete the files at the next/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "Type dev to delete it" }), "dev");
    await user.click(within(dialog).getByRole("button", { name: "Delete environment" }));

    expect(
      await screen.findByText("Deleted dev. Its files are deleted at the next clean-up."),
    ).toBeInTheDocument();
    const start = server.sent.findIndex((request) => request.path === "/api/v1/sign-in/session");
    expect(
      server.sent.slice(start, start + 4).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/environments/dev?purge=true",
    ]);
  });

  it("a cancelled touch deletes nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { dialog, user, server } = await openDelete({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });

    await user.click(within(dialog).getByRole("radio", { name: /Delete the files at the next/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "Type dev to delete it" }), "dev");
    await user.click(within(dialog).getByRole("button", { name: "Delete environment" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);
  });

  it("a deployed repository keeps the environment", async () => {
    const { dialog, user } = await openDelete({
      "DELETE /api/v1/environments/dev": {
        status: 409,
        body: {
          detail:
            "Environment 'dev' still has deployed repositories: analytics. Undeploy each first (`lakana undeploy REPO --env dev`).",
        },
      },
    });

    await user.type(within(dialog).getByRole("textbox", { name: "Type dev to delete it" }), "dev");
    await user.click(within(dialog).getByRole("button", { name: "Delete environment" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("analytics");
    expect(screen.getByRole("dialog")).toBe(dialog);
  });

  it("Cancel closes and gives the focus back", async () => {
    const { dialog, user } = await openDelete();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Delete environment dev" })).toHaveFocus();
    });
  });
});
