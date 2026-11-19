import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import { whereIs } from "../test/sentinel";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const SENTINEL = "AKIA-sentinel";

/** The environment the server answers once it is created. */
const STAGING: EnvironmentOut = {
  name: "staging",
  storage_root: "s3://corp-lake/staging",
  storage_secret_ref: null,
  is_protected: false,
  properties: {},
  retention_days: null,
  deleted_at: null,
  purge_after: null,
};

/**
 * Renders Settings › Environments for an administrator, with Create environment open.
 *
 * @param routes - More routes.
 * @returns The shell and the dialog.
 */
async function openCreate(
  routes: Record<string, FakeRoute> = {},
): Promise<ReturnType<typeof renderShell> & { readonly dialog: HTMLElement }> {
  const shell = renderShell("/settings/environments", {
    routes: {
      "GET /api/v1/environments": { status: 200, body: [] },
      "GET /api/v1/environments/staging": { status: 200, body: STAGING },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  });
  await shell.user.click(await screen.findByRole("button", { name: "Create environment" }));
  return { ...shell, dialog: await screen.findByRole("dialog") };
}

/**
 * Fills staging on S3 with one option, `key`, whose value is the sentinel.
 *
 * @param shell - The shell and the dialog.
 */
async function fillStaging(shell: Awaited<ReturnType<typeof openCreate>>): Promise<void> {
  const { dialog, user } = shell;
  await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "staging");
  await user.click(within(dialog).getByRole("radio", { name: "Amazon S3 or compatible" }));
  await user.type(within(dialog).getByRole("textbox", { name: "Root" }), "s3://corp-lake/staging");
  await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
  await user.type(within(dialog).getByRole("textbox", { name: "Key" }), "key");
  await user.type(within(dialog).getByLabelText("Value"), SENTINEL);
}

const spyConsole = (): ReturnType<typeof vi.spyOn>[] =>
  (["log", "info", "warn", "error", "debug"] as const).map((level) => vi.spyOn(console, level));

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Create environment", () => {
  it("creates after the touch, asked first", async () => {
    fakeWebAuthn();
    const shell = await openCreate({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "POST /api/v1/environments": { status: 201, body: STAGING },
    });
    await fillStaging(shell);

    await shell.user.click(
      within(shell.dialog).getByRole("button", { name: "Create environment" }),
    );

    expect(await screen.findByRole("heading", { level: 1, name: "staging" })).toBeInTheDocument();
    expect(shell.router.state.location.pathname).toBe("/settings/environments/staging");
    const start = shell.server.sent.findIndex(
      (request) => request.path === "/api/v1/sign-in/session",
    );
    expect(
      shell.server.sent
        .slice(start, start + 4)
        .map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/environments",
    ]);
    expect(shell.server.sent[start + 3]?.body).toEqual({
      name: "staging",
      storage: { scheme: "s3", root: "s3://corp-lake/staging", options: { key: SENTINEL } },
      is_protected: false,
    });
    const status = screen.getByText(/Created\. Give people access under/);
    expect(within(status).getByRole("link", { name: "Access › Grants" })).toHaveAttribute(
      "href",
      "/settings/access/grants",
    );
  });

  it("an option's value never leaves its form", async () => {
    const spies = spyConsole();
    let refuse = true;
    const shell = await openCreate({
      "POST /api/v1/environments": () =>
        refuse
          ? { status: 422, body: { detail: "Storage option key is not allowed." } }
          : { status: 201, body: STAGING },
    });
    await fillStaging(shell);
    const value = within(shell.dialog).getByLabelText<HTMLInputElement>("Value");
    expect(value.type).toBe("password");
    expect(value).toHaveAttribute("autocomplete", "new-password");
    expect(value).toHaveAttribute("data-1p-ignore");
    expect(value).toHaveAttribute("data-lpignore", "true");

    await shell.user.click(
      within(shell.dialog).getByRole("button", { name: "Create environment" }),
    );
    expect(await within(shell.dialog).findByRole("alert")).toHaveTextContent("not allowed");
    expect(within(shell.dialog).getByLabelText("Value")).toHaveValue(SENTINEL);
    expect(whereIs(SENTINEL, spies)).toEqual([]);

    refuse = false;
    await shell.user.click(
      within(shell.dialog).getByRole("button", { name: "Create environment" }),
    );
    expect(await screen.findByRole("heading", { level: 1, name: "staging" })).toBeInTheDocument();
    expect(whereIs(SENTINEL, spies)).toEqual([]);
    expect(JSON.stringify(shell.router.state.location)).not.toContain(SENTINEL);
    const carrying = shell.server.sent.filter((request) =>
      JSON.stringify(request).includes(SENTINEL),
    );
    expect(carrying.map((request) => `${request.method} ${request.path}`)).toEqual([
      "POST /api/v1/environments",
      "POST /api/v1/environments",
    ]);
  });

  it("after Cancel nothing holds an option's value", async () => {
    const spies = spyConsole();
    const shell = await openCreate();
    await fillStaging(shell);

    await shell.user.click(within(shell.dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(whereIs(SENTINEL, spies)).toEqual([]);
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("refuses empty or repeated option keys before sending", async () => {
    const { dialog, user, server } = await openCreate();
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "staging");
    await user.type(within(dialog).getByRole("textbox", { name: "Root" }), "file:///srv/x");
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
    await user.click(within(dialog).getByRole("button", { name: "Add an option" }));
    const keys = within(dialog).getAllByRole("textbox", { name: "Key" });
    await user.type(keys[0] as HTMLElement, "region");
    await user.type(keys[1] as HTMLElement, "region");

    await user.click(within(dialog).getByRole("button", { name: "Create environment" }));

    expect(
      within(dialog).getAllByText("Another option has the key region; give each its own."),
    ).toHaveLength(2);
    expect(server.sent.some((request) => request.method === "POST")).toBe(false);
    const [first] = within(dialog).getAllByRole("button", { name: "Remove the option region" });
    await user.click(first as HTMLElement);
    await user.click(within(dialog).getByRole("button", { name: "Remove the option region" }));
    expect(within(dialog).queryByRole("textbox", { name: "Key" })).not.toBeInTheDocument();
  });

  it("checks the name on leaving it, and holds the button until name and root are given", async () => {
    const { dialog, user } = await openCreate();
    const name = within(dialog).getByRole("textbox", { name: "Name" });
    const submit = within(dialog).getByRole("button", { name: "Create environment" });
    expect(submit).toBeDisabled();
    expect(within(dialog).getByText("Name it and say where its data is stored.")).toBeVisible();

    await user.click(name);
    await user.tab();
    expect(within(dialog).getByText("Name the environment.")).toBeVisible();
    await user.type(name, "dev.old");
    expect(within(dialog).getByText("A name cannot hold a dot.")).toBeVisible();
    await user.clear(name);
    await user.type(name, "staging");
    await user.type(within(dialog).getByRole("textbox", { name: "Root" }), "file:///srv/x");
    expect(submit).toBeEnabled();
  });

  it("keeps the fields with the server's sentence", async () => {
    const shell = await openCreate({
      "POST /api/v1/environments": {
        status: 409,
        body: { detail: "An environment named 'staging' already exists." },
      },
    });
    await fillStaging(shell);
    await shell.user.click(within(shell.dialog).getByRole("checkbox", { name: /Protect it/ }));

    await shell.user.click(
      within(shell.dialog).getByRole("button", { name: "Create environment" }),
    );

    const { dialog } = shell;
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already exists");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("staging");
    expect(within(dialog).getByRole("radio", { name: "Amazon S3 or compatible" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Root" })).toHaveValue(
      "s3://corp-lake/staging",
    );
    expect(within(dialog).getByRole("textbox", { name: "Key" })).toHaveValue("key");
    expect(within(dialog).getByRole("checkbox", { name: /Protect it/ })).toBeChecked();
    expect(shell.server.sent.at(-1)?.body).toMatchObject({ is_protected: true });
  });

  it("a cancelled touch creates nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const shell = await openCreate({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });
    await fillStaging(shell);

    await shell.user.click(
      within(shell.dialog).getByRole("button", { name: "Create environment" }),
    );

    expect(await within(shell.dialog).findByRole("alert")).toBeVisible();
    expect(
      shell.server.sent.some(
        (request) => request.method === "POST" && request.path === "/api/v1/environments",
      ),
    ).toBe(false);
  });

  it("the root's example follows the kind", async () => {
    const { dialog, user } = await openCreate();
    const root = within(dialog).getByRole("textbox", { name: "Root" });
    expect(root).toHaveAccessibleDescription(/file:\/\/\/srv\/lakana\/dev/);

    await user.click(within(dialog).getByRole("radio", { name: "Amazon S3 or compatible" }));
    expect(root).toHaveAccessibleDescription(/s3:\/\/bucket\/prefix/);
    await user.click(within(dialog).getByRole("radio", { name: "Azure" }));
    expect(root).toHaveAccessibleDescription(
      /abfs:\/\/container@account\.dfs\.core\.windows\.net\/prefix/,
    );
  });
});
