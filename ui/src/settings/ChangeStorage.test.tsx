import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { aSecret, freshSession } from "../test/access";
import { renderShell } from "../test/render";
import { whereIs } from "../test/sentinel";
import { type FakeRoute, whoami } from "../test/server";

const SENTINEL = "AKIA-sentinel";

/** dev, on S3 with no storage secret yet. */
const DEV: EnvironmentOut = {
  name: "dev",
  storage_root: "s3://corp-lake/dev",
  storage_secret_ref: null,
  is_protected: false,
  properties: {},
  retention_days: null,
  deleted_at: null,
  purge_after: null,
};

/** What `GET …/dev/storage` answers: two keys are set. */
const STORAGE = { root: "s3://corp-lake/dev", secret_ref: null, option_keys: ["endpoint", "key"] };

/**
 * dev's page for an administrator, with Change storage open.
 *
 * @param routes - More routes.
 * @param secretEnvironments - Where the administrator manages secrets.
 * @returns The shell and the dialog.
 */
async function openStorage(
  routes: Record<string, FakeRoute> = {},
  secretEnvironments: readonly string[] = ["dev"],
): Promise<ReturnType<typeof renderShell> & { readonly dialog: HTMLElement }> {
  const shell = renderShell("/settings/environments/dev", {
    routes: {
      "GET /api/v1/whoami": {
        status: 200,
        body: whoami("admin", true, true, true, secretEnvironments),
      },
      "GET /api/v1/environments/dev": { status: 200, body: DEV },
      "GET /api/v1/environments/dev/usage": { status: 500, body: { detail: "down" } },
      "GET /api/v1/environments/dev/storage": { status: 200, body: STORAGE },
      "GET /api/v1/environments/dev/secrets": {
        status: 200,
        body: [aSecret({ name: "corp-s3" }), aSecret({ name: "old-s3" })],
      },
      "GET /api/v1/sign-in/session": freshSession(),
      "PATCH /api/v1/environments/dev/storage": { status: 200, body: DEV },
      ...routes,
    },
  });
  await shell.user.click(await screen.findByRole("button", { name: "Change storage of dev" }));
  return { ...shell, dialog: await screen.findByRole("dialog") };
}

/**
 * The PATCH requests sent, with what came just before each.
 *
 * @param shell - The shell.
 * @returns Each PATCH body, and the path of the request before it.
 */
const patches = (shell: ReturnType<typeof renderShell>): [unknown, string | undefined][] =>
  shell.server.sent.flatMap((request, index) =>
    request.method === "PATCH"
      ? [[request.body, shell.server.sent[index - 1]?.path] as [unknown, string | undefined]]
      : [],
  );

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Change storage", () => {
  it("chooses the storage secret from the environment's secrets", async () => {
    const shell = await openStorage();
    const { dialog, user } = shell;
    const submit = within(dialog).getByRole("button", { name: "Change storage" });
    expect(submit).toBeDisabled();

    await user.click(await within(dialog).findByRole("button", { name: /Storage secret/ }));
    expect(screen.getByRole("option", { name: "Keep the current secret" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "corp-s3" }));
    await user.click(submit);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(patches(shell)).toEqual([[{ secret_ref: "corp-s3" }, "/api/v1/sign-in/session"]]);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Change storage of dev" })).toHaveFocus();
    });
  });

  it("choosing the current secret again changes nothing", async () => {
    const shell = await openStorage({
      "GET /api/v1/environments/dev": {
        status: 200,
        body: { ...DEV, storage_secret_ref: "corp-s3" },
      },
    });
    const { dialog, user } = shell;
    expect(within(dialog).getByText("corp-s3", { selector: "dd" })).toBeInTheDocument();

    await user.click(await within(dialog).findByRole("button", { name: /Storage secret/ }));
    await user.click(screen.getByRole("option", { name: "corp-s3" }));

    expect(within(dialog).getByRole("button", { name: "Change storage" })).toBeDisabled();
    expect(within(dialog).getByText("Choose another secret or replace the options.")).toBeVisible();
  });

  it("without secret rights the secret is not offered, and says what is needed", async () => {
    const shell = await openStorage({}, []);
    const { dialog } = shell;

    expect(
      within(dialog).getByText(
        "Choosing the storage secret needs the right to manage secrets in dev.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("textbox", { name: "Secret name" })).toBeNull();
    expect(within(dialog).getByRole("radio", { name: "Replace the options" })).toBeEnabled();
    expect(shell.server.sent.some((request) => request.path.endsWith("/secrets"))).toBe(false);
  });

  it("without secret rights the options of a secret-backed storage stay as they are", async () => {
    const shell = await openStorage(
      {
        "GET /api/v1/environments/dev": {
          status: 200,
          body: { ...DEV, storage_secret_ref: "corp-s3" },
        },
      },
      [],
    );
    const { dialog } = shell;

    expect(within(dialog).queryByRole("radio", { name: "Replace the options" })).toBeNull();
    expect(
      within(dialog).getByText(
        "Changing the options of a storage with a secret needs the right to manage secrets in dev: an option such as s3.endpoint decides where the secret is sent.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Change storage" })).toBeDisabled();
  });

  it("keeps the options unless told to replace them", async () => {
    const shell = await openStorage();
    const { dialog, user } = shell;
    expect(await within(dialog).findByText("Set now: endpoint, key")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Add an option" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("radio", { name: "Replace the options" }));
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Key" }), "region");
    await user.type(within(dialog).getByLabelText("Value"), "eu-central-1");
    await user.click(within(dialog).getByRole("button", { name: "Change storage" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(patches(shell)).toEqual([
      [{ options: { region: "eu-central-1" } }, "/api/v1/sign-in/session"],
    ]);
  });

  it("an option's value never leaves its form", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level),
    );
    let refuse = true;
    const shell = await openStorage({
      "PATCH /api/v1/environments/dev/storage": () =>
        refuse
          ? { status: 422, body: { detail: "Storage option key is not allowed." } }
          : { status: 200, body: DEV },
    });
    const { dialog, user } = shell;
    await user.click(within(dialog).getByRole("radio", { name: "Replace the options" }));
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Key" }), "key");
    await user.type(within(dialog).getByLabelText("Value"), SENTINEL);

    await user.click(within(dialog).getByRole("button", { name: "Change storage" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("not allowed");
    expect(within(dialog).getByLabelText("Value")).toHaveValue(SENTINEL);
    expect(whereIs(SENTINEL, spies)).toEqual([]);

    refuse = false;
    await user.click(within(dialog).getByRole("button", { name: "Change storage" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(whereIs(SENTINEL, spies)).toEqual([]);
    expect(JSON.stringify(shell.router.state.location)).not.toContain(SENTINEL);
    const carrying = shell.server.sent.filter((request) =>
      JSON.stringify(request).includes(SENTINEL),
    );
    expect(carrying.map((request) => `${request.method} ${request.path}`)).toEqual([
      "PATCH /api/v1/environments/dev/storage",
      "PATCH /api/v1/environments/dev/storage",
    ]);
  });

  it("a refusal of the options is shown with the fields kept", async () => {
    const shell = await openStorage({
      "GET /api/v1/environments/dev/storage": {
        status: 200,
        body: { ...STORAGE, option_keys: [] },
      },
      "PATCH /api/v1/environments/dev/storage": {
        status: 403,
        body: {
          detail: "Changing the storage options of dev needs the right to manage secrets in it.",
        },
      },
    });
    const { dialog, user } = shell;
    expect(await within(dialog).findByText("No option is set now.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: "Replace the options" }));
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Key" }), "region");

    await user.click(within(dialog).getByRole("button", { name: "Change storage" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("manage secrets in it");
    expect(within(dialog).getByRole("textbox", { name: "Key" })).toHaveValue("region");
    expect(within(dialog).getByRole("radio", { name: "Replace the options" })).toBeChecked();
  });

  it("names an empty key before sending", async () => {
    const shell = await openStorage();
    const { dialog, user } = shell;
    await user.click(within(dialog).getByRole("radio", { name: "Replace the options" }));
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));

    await user.click(within(dialog).getByRole("button", { name: "Change storage" }));

    expect(within(dialog).getByText("Name the key.")).toBeVisible();
    expect(patches(shell)).toEqual([]);
  });

  it("says why the secret or the keys cannot be read, and reads them again", async () => {
    let fail = true;
    const shell = await openStorage({
      "GET /api/v1/environments/dev/storage": () =>
        fail ? { status: 500, body: { detail: "down" } } : { status: 200, body: STORAGE },
      "GET /api/v1/environments/dev/secrets": () =>
        fail ? { status: 500, body: { detail: "down" } } : { status: 200, body: [] },
    });
    const { dialog, user } = shell;

    const retries = await within(dialog).findAllByRole("button", { name: "Try again" });
    expect(retries).toHaveLength(2);
    fail = false;
    for (const retry of retries) await user.click(retry);

    expect(await within(dialog).findByText("Set now: endpoint, key")).toBeInTheDocument();
    expect(
      await within(dialog).findByText(
        "This environment has no secret yet. Add one under Access › Secrets.",
      ),
    ).toBeInTheDocument();
  });

  it("a protected environment names Unprotect instead of Change storage", async () => {
    renderShell("/settings/environments/dev", {
      routes: {
        "GET /api/v1/environments/dev": {
          status: 200,
          body: { ...DEV, is_protected: true, storage_secret_ref: "corp-s3" },
        },
      },
    });

    expect(
      await screen.findByText(
        "The storage of a protected environment cannot be changed. Unprotect it first.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change storage of dev" })).not.toBeInTheDocument();
  });
});
