import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { anEnvironment, freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { renderShell } from "../test/render";
import type { FakeRoute, FakeServer } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const ED25519 =
  "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl";

/** An SSH repository on GitHub with the deploy key of dev; prod has none. */
const ANALYTICS = {
  id: "r-1",
  name: "analytics",
  kind: "ssh",
  url: "git@github.com:acme/analytics.git",
  credential_secret: "analytics-deploy-key",
  credential_username: null,
  ssh_known_hosts: `${ED25519}\n`,
  default_ref: "main",
  created_by: null,
  created_at: "2026-11-01T09:00:00Z",
  updated_at: "2026-11-02T09:00:00Z",
  deploy_keys: [
    { environment: "dev", public_key: "ssh-ed25519 AAAA old-dev", fingerprint: "SHA256:old" },
  ],
  host_keys: [{ type: "ssh-ed25519", fingerprint: "SHA256:host" }],
};

const NEW_KEY = {
  secret: "analytics-deploy-key",
  keys: [
    {
      environment: "dev",
      public_key: "ssh-ed25519 AAAA new-dev",
      fingerprint: "SHA256:new",
      made: true,
    },
  ],
  skipped: [],
  replaced: ["dev"],
  notes: ["The old key of dev stops working now: replace it at your git host."],
};

/**
 * Renders the page of analytics for an administrator who manages secrets in dev and prod.
 *
 * @param routes - More routes.
 * @param repo - What the server answers for the repository.
 * @returns The rendered shell.
 */
function renderPage(
  routes: Record<string, FakeRoute>,
  repo: unknown = ANALYTICS,
): ReturnType<typeof renderShell> {
  return renderShell("/settings/repositories/analytics", {
    routes: {
      "GET /api/v1/repositories/analytics": { status: 200, body: repo },
      "GET /api/v1/environments": {
        status: 200,
        body: [anEnvironment("dev"), anEnvironment("prod")],
      },
      "GET /api/v1/sign-in/session": freshSession(),
      ...routes,
    },
  });
}

/**
 * The requests as `METHOD path`, from the first session read on.
 *
 * @param sent - The requests.
 * @returns The calls.
 */
function fromSession(sent: FakeServer["sent"]): string[] {
  const start = sent.findIndex((request) => request.path === "/api/v1/sign-in/session");
  return sent.slice(start).map((request) => `${request.method} ${request.path}`);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RepositoryDialogs", () => {
  it("edits the default ref after the touch, asked first", async () => {
    const { server, user } = renderPage({
      "PATCH /api/v1/repositories/analytics": { status: 200, body: ANALYTICS },
    });
    const edit = await screen.findByRole("button", { name: "Edit repository analytics" });
    await user.click(edit);
    const dialog = await screen.findByRole("dialog");
    const ref = within(dialog).getByRole("textbox", { name: "Default ref" });
    await user.clear(ref);
    await user.type(ref, "release");

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const calls = fromSession(server.sent);
    expect(calls.slice(0, 3)).toEqual([
      "GET /api/v1/sign-in/session",
      "PATCH /api/v1/repositories/analytics",
      "GET /api/v1/repositories/analytics",
    ]);
    const patch = server.sent.find((request) => request.method === "PATCH");
    expect(patch?.body).toEqual({ default_ref: "release" });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Edit repository analytics" })).toHaveFocus();
    });
  });

  it("clears an https repository's username and default ref", async () => {
    const https = {
      ...ANALYTICS,
      kind: "https",
      url: "https://github.com/acme/analytics.git",
      credential_secret: "gh-token",
      credential_username: "x-access-token",
      deploy_keys: [],
    };
    const { server, user } = renderPage(
      { "PATCH /api/v1/repositories/analytics": { status: 200, body: https } },
      https,
    );
    await user.click(await screen.findByRole("button", { name: "Edit repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByRole("textbox", { name: "Username" }));
    await user.clear(within(dialog).getByRole("textbox", { name: "Default ref" }));

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PATCH")?.body).toStrictEqual({
        default_ref: null,
        credential_username: null,
      });
    });
  });

  it("host keys are fetched and ticked, never typed", async () => {
    const found = {
      id: "p-1",
      kind: "host_keys",
      state: "done",
      outcome: "ok",
      message: "Found 1 key.",
      host_keys: [{ type: "ssh-ed25519", fingerprint: "SHA256:host", line: ED25519 }],
      created_at: "2026-11-18T08:00:00Z",
      finished_at: "2026-11-18T08:00:01Z",
    };
    const { server, user } = renderPage({
      "POST /api/v1/repository-probes": { status: 202, body: found },
      "PATCH /api/v1/repositories/analytics": { status: 200, body: ANALYTICS },
    });
    await user.click(await screen.findByRole("button", { name: "Edit repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("textbox", { name: /known/i })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Fetch host keys again" }));
    const tick = await within(dialog).findByRole("checkbox", { name: "This matches: ssh-ed25519" });
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      kind: "host_keys",
      url: "git@github.com:acme/analytics.git",
    });
    await user.click(tick);
    await user.click(tick);
    await user.click(tick);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({
        ssh_known_hosts: `${ED25519}\n`,
      });
    });
  });

  it("an unticked key is not sent, and an empty answer says so", async () => {
    const found = {
      id: "p-1",
      kind: "host_keys",
      state: "done",
      outcome: "ok",
      message: "Found 0 keys.",
      host_keys: [],
      created_at: "2026-11-18T08:00:00Z",
      finished_at: "2026-11-18T08:00:01Z",
    };
    const { server, user } = renderPage(
      {
        "POST /api/v1/repository-probes": { status: 202, body: found },
        "PATCH /api/v1/repositories/analytics": {
          status: 422,
          body: { detail: "nothing to change" },
        },
      },
      { ...ANALYTICS, credential_secret: null, default_ref: null },
    );
    await user.click(await screen.findByRole("button", { name: "Edit repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Fetch host keys again" }));

    expect(
      await within(dialog).findByText(
        "The host offered no SSH key. Check the URL, then fetch again.",
      ),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await within(dialog).findByText("nothing to change");
    expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({});
  });

  it("keeps the fields with the server's sentence", async () => {
    const { user } = renderPage({
      "PATCH /api/v1/repositories/analytics": {
        status: 422,
        body: { detail: "url: not a repository URL; enter it as git@host:team/repo.git" },
      },
    });
    await user.click(await screen.findByRole("button", { name: "Edit repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    const url = within(dialog).getByRole("textbox", { name: "URL" });
    await user.clear(url);
    await user.type(url, "github.com/acme");

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("not a repository URL");
    expect(url).toHaveValue("github.com/acme");
    expect(within(dialog).getByRole("textbox", { name: "Default ref" })).toHaveValue("main");
  });

  it("replaces one environment's key after the touch, asked first", async () => {
    fakeWebAuthn();
    const { server, user } = renderPage({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "POST /api/v1/repositories/analytics/deploy-key": { status: 201, body: NEW_KEY },
    });
    expect(await screen.findByRole("button", { name: "Replace key of dev" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replace key of prod" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Replace key of dev" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("The old key stops working at once.");
    await user.click(within(dialog).getByRole("button", { name: "Replace key" }));

    expect(await within(dialog).findByText("SHA256:new")).toBeInTheDocument();
    expect(fromSession(server.sent).slice(0, 4)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/repositories/analytics/deploy-key",
    ]);
    expect(server.sent.find((request) => request.path.endsWith("/deploy-key"))?.body).toEqual({
      environments: ["dev"],
      replace: true,
    });
    expect(within(dialog).getByRole("button", { name: "Copy key" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: /deploy-key page/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/analytics/settings/keys/new",
    );
    expect(within(dialog).getByText(NEW_KEY.notes[0] as string)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 3, name: "Deploy keys" })).toHaveFocus();
    });
  });

  it("a cancelled touch replaces nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { server, user } = renderPage({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });
    await user.click(await screen.findByRole("button", { name: "Replace key of dev" }));
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Replace key" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(server.sent.some((request) => request.path.endsWith("/deploy-key"))).toBe(false);
  });

  it("offers no missing keys where the person cannot see whether a key exists", async () => {
    renderPage({
      "GET /api/v1/environments": { status: 200, body: [anEnvironment("dev")] },
    });

    expect(await screen.findByRole("button", { name: "Replace key of dev" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make the missing keys" })).not.toBeInTheDocument();
  });

  it("makes the missing keys and names the skipped", async () => {
    const made = {
      ...NEW_KEY,
      keys: [{ ...NEW_KEY.keys[0], environment: "prod" }],
      skipped: ["staging"],
      replaced: [],
      notes: [],
    };
    const { server, user } = renderPage({
      "POST /api/v1/repositories/analytics/deploy-key": { status: 201, body: made },
    });
    await user.click(await screen.findByRole("button", { name: "Make the missing keys" }));
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Make the keys" }));

    expect(await within(dialog).findByText(/No key in staging/)).toBeInTheDocument();
    expect(server.sent.find((request) => request.path.endsWith("/deploy-key"))?.body).toEqual({});
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("deletes after the name is typed, without a touch", async () => {
    const { server, user } = renderPage({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      "DELETE /api/v1/repositories/analytics": { status: 204 },
      "GET /api/v1/repositories": { status: 200, body: [] },
    });
    await user.click(await screen.findByRole("button", { name: "Delete repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Jobs already deployed keep running until undeployed");
    const submit = within(dialog).getByRole("button", { name: "Delete repository" });
    expect(submit).toBeDisabled();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Type analytics to delete it" }),
      "analytics",
    );

    await user.click(submit);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Repositories" }),
    ).toBeInTheDocument();
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Repositories" })).toHaveFocus();
    });
  });

  it("a deployed repository stays, with the next step", async () => {
    const { user } = renderPage({
      "DELETE /api/v1/repositories/analytics": {
        status: 409,
        body: { detail: "prod still deploys analytics: undeploy it there first" },
      },
    });
    await user.click(await screen.findByRole("button", { name: "Delete repository analytics" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(
      within(dialog).getByRole("textbox", { name: "Type analytics to delete it" }),
      "analytics",
    );

    await user.click(within(dialog).getByRole("button", { name: "Delete repository" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "prod still deploys analytics: undeploy it there first",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Cancel closes a dialog and changes nothing", async () => {
    const { server, user } = renderPage({});
    await user.click(await screen.findByRole("button", { name: "Delete repository analytics" }));
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(server.sent.filter((request) => request.method !== "GET")).toEqual([]);
  });
});
