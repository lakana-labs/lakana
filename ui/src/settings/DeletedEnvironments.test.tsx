import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { areasFor } from "../shell/areas";
import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";
import { unreachable } from "./DeletedEnvironments";

/** A deleted environment whose files are kept a week longer. */
const DEV_OLD: EnvironmentOut = {
  name: "dev-old",
  storage_root: "s3://corp-lake/dev-old",
  storage_secret_ref: null,
  is_protected: false,
  properties: {},
  retention_days: null,
  deleted_at: "2026-11-10T09:00:00Z",
  purge_after: "2099-11-27T09:00:00Z",
};

/** A deleted environment an older release stored on HDFS, which Lakana never reached. */
const HDFS_OLD: EnvironmentOut = { ...DEV_OLD, name: "hdfs-old", storage_root: "hdfs://old/x" };

/**
 * The two lists, which the actions below change.
 *
 * @param live - The live environments.
 * @param deleted - The deleted environments.
 * @returns The route.
 */
function lists(live: EnvironmentOut[], deleted: EnvironmentOut[]): Record<string, FakeRoute> {
  return {
    "GET /api/v1/environments": (request) => ({
      status: 200,
      body: request.path.endsWith("?deleted=true") ? deleted : live,
    }),
  };
}

/**
 * The options of mara, who manages dev-old and hdfs-old and nothing on the instance.
 *
 * @param routes - More routes.
 * @returns The options of `renderShell`.
 */
function asMara(routes: Record<string, FakeRoute>): Parameters<typeof renderShell>[1] {
  return {
    access: areasFor(false),
    me: { name: "mara", admin: false },
    routes: {
      "GET /api/v1/whoami": {
        status: 200,
        body: whoami("mara", false, true, false, [], false, {
          readsEnvironments: true,
          managedEnvironments: ["dev-old", "hdfs-old"],
        }),
      },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  };
}

/**
 * The options of an administrator, who manages both and everything on the instance.
 *
 * @param routes - More routes.
 * @returns The options of `renderShell`.
 */
function asAdmin(routes: Record<string, FakeRoute>): Parameters<typeof renderShell>[1] {
  return {
    routes: {
      "GET /api/v1/whoami": {
        status: 200,
        body: whoami("admin", true, true, true, [], true, {
          managedEnvironments: ["dev-old", "hdfs-old"],
        }),
      },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  };
}

describe("DeletedEnvironments", () => {
  it("restores without a touch and puts the focus on it", async () => {
    const live: EnvironmentOut[] = [];
    const deleted = [DEV_OLD];
    const { server, user } = renderShell(
      "/settings/environments",
      asMara({
        ...lists(live, deleted),
        "POST /api/v1/environments/dev-old/restore": () => {
          deleted.splice(0);
          live.push({ ...DEV_OLD, deleted_at: null, purge_after: null });
          return { status: 200, body: live[0] };
        },
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Restore dev-old" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore dev-old?" });
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));

    const link = await screen.findByRole("link", { name: "dev-old" });
    await waitFor(() => {
      expect(link).toHaveFocus();
    });
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("Restored dev-old.");
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
    expect(screen.queryByRole("heading", { name: "Deleted environments" })).not.toBeInTheDocument();
  });

  it("deletes the files now after the name and the touch", async () => {
    fakeWebAuthn();
    const deleted = [DEV_OLD];
    const { server, user } = renderShell(
      "/settings/environments",
      asMara({
        ...lists([], deleted),
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "DELETE /api/v1/environments/dev-old": () => {
          // The files are due now: the row stays until the clean-up deletes them.
          // The server's clock is a minute behind the browser's: the row is due all the same.
          deleted[0] = { ...DEV_OLD, purge_after: new Date(Date.now() + 60_000).toISOString() };
          return { status: 200, body: deleted[0] };
        },
      }),
    );

    await user.click(
      await screen.findByRole("button", { name: "Delete the files of dev-old now" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete the files of dev-old now?" });
    const submit = within(dialog).getByRole("button", { name: "Delete the files" });
    expect(submit).toBeDisabled();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Type dev-old to delete its files" }),
      "dev-old",
    );
    await user.click(submit);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Deleted environments" })).toHaveFocus();
    });
    expect(
      screen.getByText("The files of dev-old are deleted at the next clean-up."),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Its files are deleted at the next clean-up."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore dev-old" })).not.toBeInTheDocument();
    const start = server.sent.findIndex((request) => request.path === "/api/v1/sign-in/session");
    expect(
      server.sent.slice(start, start + 4).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/environments/dev-old?purge=true",
    ]);
  });

  it("a later deletion of the same name is not due because an earlier one's files were", async () => {
    fakeWebAuthn();
    const deleted = [DEV_OLD, HDFS_OLD];
    const live: EnvironmentOut[] = [];
    const { user } = renderShell(
      "/settings/environments",
      asAdmin({
        ...lists(live, deleted),
        "DELETE /api/v1/environments/hdfs-old": () => {
          deleted[1] = { ...HDFS_OLD, purge_after: new Date(Date.now() + 60_000).toISOString() };
          return { status: 200, body: deleted[1] };
        },
        "POST /api/v1/environments/dev-old/restore": () => {
          // Meanwhile, elsewhere, hdfs-old's rows were removed and the name was created and
          // deleted again: a new deletion, restorable for a week.
          deleted.splice(0, 2, { ...HDFS_OLD, deleted_at: "2026-11-19T10:00:00Z" });
          live.push({ ...DEV_OLD, deleted_at: null, purge_after: null });
          return { status: 200, body: live[0] };
        },
      }),
    );

    await user.click(
      await screen.findByRole("button", { name: "Delete the files of hdfs-old now" }),
    );
    const purge = await screen.findByRole("dialog");
    await user.type(
      within(purge).getByRole("textbox", { name: "Type hdfs-old to delete its files" }),
      "hdfs-old",
    );
    await user.click(within(purge).getByRole("button", { name: "Delete the files" }));
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Restore hdfs-old" })).not.toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: "Restore dev-old" }));
    const restore = await screen.findByRole("dialog", { name: "Restore dev-old?" });
    await user.click(within(restore).getByRole("button", { name: "Restore" }));

    expect(await screen.findByRole("link", { name: "dev-old" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Restore hdfs-old" })).toBeInTheDocument();
  });

  it("offers Remove from the catalog only for storage Lakana cannot reach", async () => {
    const deleted = [DEV_OLD, HDFS_OLD];
    const { server, user } = renderShell(
      "/settings/environments",
      asAdmin({
        ...lists([], deleted),
        "POST /api/v1/environments/hdfs-old/purge-rows": () => {
          deleted.splice(1);
          return { status: 204 };
        },
      }),
    );

    expect(
      await screen.findByRole("button", { name: "Remove hdfs-old from the catalog" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove dev-old from the catalog" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove hdfs-old from the catalog" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove hdfs-old from the catalog?" });
    expect(dialog).toHaveTextContent("Lakana cannot reach this storage.");
    await user.click(within(dialog).getByRole("button", { name: "Remove from the catalog" }));

    expect(await screen.findByText("Removed hdfs-old from the catalog.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Deleted environments" })).toHaveFocus();
    });
    const post = server.sent.findIndex((request) => request.path.endsWith("/purge-rows"));
    expect(server.sent[post - 1]?.path).toBe("/api/v1/sign-in/session");
  });

  it("the focus goes to the heading when the last deleted one is removed", async () => {
    const deleted = [HDFS_OLD];
    const { user } = renderShell(
      "/settings/environments",
      asAdmin({
        ...lists([], deleted),
        "POST /api/v1/environments/hdfs-old/purge-rows": () => {
          deleted.splice(0);
          return { status: 204 };
        },
      }),
    );

    await user.click(
      await screen.findByRole("button", { name: "Remove hdfs-old from the catalog" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove from the catalog" }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Environments" })).toHaveFocus();
    });
  });

  it("a deleted environment's actions follow the rights", async () => {
    renderShell("/settings/environments", asMara(lists([], [DEV_OLD, HDFS_OLD])));

    expect(await screen.findByRole("button", { name: "Restore hdfs-old" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete the files of hdfs-old now" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Remove hdfs-old from the catalog" }),
    ).not.toBeInTheDocument();
  });

  it("offers no Restore once the files are due, and says so", async () => {
    renderShell(
      "/settings/environments",
      asMara(lists([], [{ ...DEV_OLD, purge_after: "2026-01-01T00:00:00Z" }])),
    );

    expect(
      await screen.findByText("Its files are deleted at the next clean-up."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore dev-old" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete the files of dev-old now" }),
    ).not.toBeInTheDocument();
  });

  it("a refused restore stays with the next step", async () => {
    const { user } = renderShell(
      "/settings/environments",
      asMara({
        ...lists([], [DEV_OLD]),
        "POST /api/v1/environments/dev-old/restore": {
          status: 409,
          body: { detail: "The clean-up has started deleting the files of dev-old." },
        },
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Restore dev-old" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("clean-up has started");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Restore dev-old" })).toHaveFocus();
    });
  });

  it("says which roots Lakana cannot reach", () => {
    expect(unreachable("hdfs://old/x")).toBe(true);
    expect(unreachable("local:///data")).toBe(true);
    expect(unreachable("s3://corp-lake/x")).toBe(false);
    expect(unreachable("file:///srv/x")).toBe(false);
    expect(unreachable("simplecache::s3://corp-lake/x")).toBe(false);
  });
});
