import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { areasFor } from "../shell/areas";
import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

/** dev, on S3 with its secret, keeping its tables. */
const DEV: EnvironmentOut = {
  name: "dev",
  storage_root: "s3://corp-lake/dev",
  storage_secret_ref: "corp-s3",
  is_protected: false,
  properties: { owner: "data-platform" },
  retention_days: null,
  deleted_at: null,
  purge_after: null,
};

/**
 * How much dev uses.
 *
 * @param complete - Whether every size is measured.
 * @returns The usage.
 */
const usage = (complete = true): unknown => ({
  environment: "dev",
  tables: { count: 3, bytes: 1_000_000_000, unmeasured: complete ? 0 : 1 },
  dropped_tables: { count: 1, bytes: 200_000_000, unmeasured: 0 },
  artifacts: { count: 0, bytes: 0, unmeasured: 0 },
  code_archives: { count: 2, bytes: 4_000, unmeasured: 0 },
  total_bytes: 1_200_004_000,
  complete,
});

/**
 * dev's page as an administrator sees it.
 *
 * @param routes - More routes.
 * @param environment - What the server answers for dev.
 * @returns The shell.
 */
function asAdmin(
  routes: Record<string, FakeRoute> = {},
  environment: EnvironmentOut = DEV,
): ReturnType<typeof renderShell> {
  return renderShell("/settings/environments/dev", {
    routes: {
      "GET /api/v1/environments/dev": { status: 200, body: environment },
      "GET /api/v1/environments/dev/usage": { status: 200, body: usage() },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  });
}

/**
 * The options of a person who is no administrator.
 *
 * @param name - Their name.
 * @param managed - The environments they manage.
 * @param routes - More routes.
 * @returns The options of `renderShell`.
 */
function asPerson(
  name: string,
  managed: readonly string[],
  routes: Record<string, FakeRoute>,
): Parameters<typeof renderShell>[1] {
  return {
    access: areasFor(false),
    me: { name, admin: false },
    routes: {
      "GET /api/v1/whoami": {
        status: 200,
        body: whoami(name, false, true, false, [], false, {
          readsEnvironments: true,
          managedEnvironments: managed,
        }),
      },
      "GET /api/v1/environments/dev": { status: 200, body: DEV },
      "GET /api/v1/environments/dev/usage": { status: 200, body: usage() },
      ...routes,
    },
  };
}

describe("EnvironmentPage", () => {
  it("shows storage, protection, retention and properties to a reader", async () => {
    const { server } = renderShell("/settings/environments/dev", asPerson("kim", [], {}));

    expect(await screen.findByText("S3 · s3://corp-lake/dev")).toBeInTheDocument();
    expect(screen.getByText("not protected")).toBeInTheDocument();
    expect(screen.getByText("keeps tables")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
    expect(screen.getByText("data-platform")).toBeInTheDocument();
    expect(screen.getByText(/Properties are set when an environment is created/)).toBeVisible();
    expect(screen.queryByText("corp-s3")).not.toBeInTheDocument();
    expect(within(screen.getByRole("main")).queryByRole("button")).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.endsWith("/usage"))).toBe(false);
  });

  it("an environment manager is not an instance manager", async () => {
    renderShell("/settings/environments/dev", asPerson("mara", ["dev"], {}));

    expect(await screen.findByRole("button", { name: "Set retention of dev" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "Usage" })).toBeInTheDocument();
    expect(screen.getByText("corp-s3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Protect dev" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Storage" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Leftover directories" })).not.toBeInTheDocument();
  });

  it("protects after the touch, with no confirmation", async () => {
    fakeWebAuthn();
    let protectedNow = false;
    const { server, user } = asAdmin({
      "GET /api/v1/environments/dev": () => ({
        status: 200,
        body: { ...DEV, is_protected: protectedNow },
      }),
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "PATCH /api/v1/environments/dev": () => {
        protectedNow = true;
        return { status: 200, body: { ...DEV, is_protected: true } };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Protect dev" }));

    const unprotect = await screen.findByRole("button", { name: "Unprotect dev" });
    await waitFor(() => {
      expect(unprotect).toHaveFocus();
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const start = server.sent.findIndex((request) => request.path === "/api/v1/sign-in/session");
    expect(
      server.sent.slice(start, start + 4).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "PATCH /api/v1/environments/dev",
    ]);
    expect(server.sent[start + 3]?.body).toEqual({ is_protected: true });
    expect(screen.getByText("protected")).toHaveClass("env-protected");
  });

  it("unprotecting asks first, then the touch", async () => {
    let protectedNow = true;
    const { server, user } = asAdmin({
      "GET /api/v1/environments/dev": () => ({
        status: 200,
        body: { ...DEV, is_protected: protectedNow },
      }),
      "PATCH /api/v1/environments/dev": () => {
        protectedNow = false;
        return { status: 200, body: DEV };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Unprotect dev" }));
    const dialog = await screen.findByRole("dialog", { name: "Unprotect dev?" });
    expect(dialog).toHaveTextContent("Deleting it, and a retention that expires its tables");
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Unprotect" }));

    const protect = await screen.findByRole("button", { name: "Protect dev" });
    await waitFor(() => {
      expect(protect).toHaveFocus();
    });
    const patch = server.sent.findIndex((request) => request.method === "PATCH");
    expect(server.sent[patch - 1]?.path).toBe("/api/v1/sign-in/session");
    expect(server.sent[patch]?.body).toEqual({ is_protected: false });
  });

  it("a cancelled touch protects nothing and says so", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { server, user } = asAdmin({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });

    await user.click(await screen.findByRole("button", { name: "Protect dev" }));

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("sets the retention without a touch", async () => {
    let days: number | null = null;
    const { server, user } = asAdmin({
      "GET /api/v1/environments/dev": () => ({
        status: 200,
        body: { ...DEV, retention_days: days },
      }),
      "PUT /api/v1/environments/dev/retention": (request) => {
        days = (request.body as { days: number | null }).days;
        return { status: 200, body: { ...DEV, retention_days: days } };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Set retention of dev" }));
    let dialog = await screen.findByRole("dialog", { name: "Set retention of dev" });
    await user.click(within(dialog).getByRole("radio", { name: "Tables expire after" }));
    const field = within(dialog).getByRole("textbox", { name: "Days" });
    expect(field).toHaveValue("30");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("tables expire after 30 days")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Set retention of dev" });
    await waitFor(() => {
      expect(button).toHaveFocus();
    });
    await user.click(button);
    dialog = await screen.findByRole("dialog", { name: "Set retention of dev" });
    expect(within(dialog).getByRole("radio", { name: "Tables expire after" })).toBeChecked();
    await user.click(within(dialog).getByRole("radio", { name: "Keep tables" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("keeps tables")).toBeInTheDocument();
    const puts = server.sent.filter((request) => request.method === "PUT");
    expect(puts.map((request) => request.body)).toEqual([{ days: 30 }, { days: null }]);
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("a refused retention keeps the dialog", async () => {
    const { user } = asAdmin(
      {
        "PUT /api/v1/environments/dev/retention": {
          status: 422,
          body: { detail: "Environment dev is protected: its tables never expire." },
        },
      },
      { ...DEV, is_protected: true },
    );

    await user.click(await screen.findByRole("button", { name: "Set retention of dev" }));
    const dialog = await screen.findByRole("dialog", { name: "Set retention of dev" });
    await user.click(within(dialog).getByRole("radio", { name: "Tables expire after" }));
    const field = within(dialog).getByRole("textbox", { name: "Days" });
    await user.clear(field);
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(field, "45");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("is protected");
    expect(within(dialog).getByRole("textbox", { name: "Days" })).toHaveValue("45");
  });

  it("shows usage with its parts, and the incomplete sentence", async () => {
    asAdmin(
      { "GET /api/v1/environments/dev/usage": { status: 200, body: usage(false) } },
      { ...DEV, storage_secret_ref: null },
    );
    expect(await screen.findByText("none", { selector: "dd" })).toBeInTheDocument();

    const panel = (await screen.findByRole("heading", { level: 2, name: "Usage" })).closest(
      "section",
    ) as HTMLElement;
    expect(await within(panel).findByText("3 · 1 GB")).toBeInTheDocument();
    expect(within(panel).getByText("1 · 200 MB")).toBeInTheDocument();
    expect(within(panel).getByText("0 · 0 B")).toBeInTheDocument();
    expect(within(panel).getByText("2 · 4 kB")).toBeInTheDocument();
    expect(within(panel).getByText("at least 1.2 GB")).toBeInTheDocument();
    expect(within(panel).getByText(/Some sizes are not measured yet/)).toBeVisible();
  });

  it("a failed usage read says so and can be read again", async () => {
    let fail = true;
    const { user } = asAdmin({
      "GET /api/v1/environments/dev/usage": () =>
        fail ? { status: 500, body: { detail: "down" } } : { status: 200, body: usage() },
    });

    const retry = await screen.findByRole("button", { name: "Try again" });
    fail = false;
    await user.click(retry);

    expect(await screen.findByText("1.2 GB")).toBeInTheDocument();
  });

  it("a deleted environment's address says so", async () => {
    const gone = {
      "GET /api/v1/environments/dev": {
        status: 404,
        body: { detail: "Environment 'dev' is deleted; restore it with lakana env restore dev." },
      },
    };
    renderShell("/settings/environments/dev", asPerson("mara", ["dev"], gone));

    expect(await screen.findByRole("alert")).toHaveTextContent("restore it");
    expect(screen.getByRole("link", { name: "See the deleted environments" })).toHaveAttribute(
      "href",
      "/settings/environments#deleted",
    );
  });

  it("a reader of a missing environment gets the plain sentence and the way back", async () => {
    renderShell(
      "/settings/environments/dev",
      asPerson("kim", [], {
        "GET /api/v1/environments/dev": {
          status: 404,
          body: { detail: "No environment named 'dev'." },
        },
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("No environment named 'dev'.");
    expect(screen.getByRole("link", { name: "Back to Environments" })).toHaveAttribute(
      "href",
      "/settings/environments",
    );
  });

  it("a failed read keeps the focus on the heading", async () => {
    let fail = true;
    const { user } = asAdmin({
      "GET /api/v1/environments/dev": () =>
        fail ? { status: 500, body: { detail: "down" } } : { status: 200, body: DEV },
    });

    const retry = await screen.findByRole("button", { name: "Try again" });
    fail = false;
    await user.click(retry);

    expect(screen.getByRole("heading", { level: 1, name: "dev" })).toHaveFocus();
    expect(await screen.findByText("S3 · s3://corp-lake/dev")).toBeInTheDocument();
  });
});
