import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import { anEnvironment } from "../test/access";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";

const PATH = "/settings/repositories/analytics";

/** An SSH repository on GitHub with two trusted host keys and the deploy key of dev. */
const ANALYTICS = {
  id: "r-1",
  name: "analytics",
  kind: "ssh",
  url: "git@github.com:acme/analytics.git",
  credential_secret: "analytics-deploy-key",
  credential_username: null,
  ssh_known_hosts: "github.com ssh-ed25519 AAAA\ngithub.com ecdsa-sha2-nistp256 BBBB\n",
  default_ref: "main",
  created_by: null,
  created_at: "2026-11-01T09:00:00Z",
  updated_at: "2026-11-02T09:00:00Z",
  deploy_keys: [
    {
      environment: "dev",
      public_key: "ssh-ed25519 AAAAC3Nza lakana-analytics-dev",
      fingerprint: "SHA256:devkey",
    },
  ],
  host_keys: [
    {
      type: "ecdsa-sha2-nistp256",
      fingerprint: "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
    },
    { type: "ssh-ed25519", fingerprint: "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" },
  ],
};

const DONE = {
  id: "p-1",
  kind: "access",
  state: "done",
  outcome: "auth_refused",
  message: "GitHub refused the deploy key of prod. Add the key at the repository's deploy keys.",
  host_keys: null,
  created_at: "2026-11-18T08:00:00Z",
  finished_at: "2026-11-18T08:00:01Z",
};

/**
 * Renders the page of a repository.
 *
 * @param repo - What the server answers for it.
 * @param more - Who reads it, and more routes.
 * @param more.reader - Whether a person who reads dev and manages nothing reads it.
 * @param more.routes - More routes.
 * @returns The rendered shell.
 */
function renderPage(
  repo: unknown = ANALYTICS,
  more: { readonly reader?: boolean; readonly routes?: Record<string, FakeRoute> } = {},
): ReturnType<typeof renderShell> {
  const routes: Record<string, FakeRoute> = {
    "GET /api/v1/repositories/analytics": { status: 200, body: repo },
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    ...more.routes,
  };
  return more.reader === true
    ? renderShell(PATH, {
        access: areasFor(false),
        me: { name: "kim", admin: false },
        routes: {
          "GET /api/v1/whoami": {
            status: 200,
            body: whoami("kim", false, true, false, [], false, { readsEnvironments: true }),
          },
          ...routes,
        },
      })
    : renderShell(PATH, { routes });
}

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("RepositoryPage", () => {
  it("shows an ssh repository's URL, host keys and deploy keys", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { level: 1, name: "analytics" })).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "href",
      "/settings/repositories",
    );
    expect(await screen.findByRole("link", { name: "Open in your git host" })).toHaveAttribute(
      "href",
      "https://github.com/acme/analytics.git",
    );
    expect(
      screen.getByText("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"),
    ).toBeInTheDocument();
    expect(screen.getByText("ecdsa-sha2-nistp256")).toBeInTheDocument();
    expect(screen.getByText("Deploy key in 1 environment")).toBeInTheDocument();
    expect(screen.getByText("SHA256:devkey")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy public key" })).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("shows an https repository's secret and username", async () => {
    renderPage({
      ...ANALYTICS,
      kind: "https",
      url: "https://gitlab.internal/data/analytics.git",
      credential_secret: "gl-token",
      default_ref: null,
      deploy_keys: [],
      host_keys: [],
    });

    expect(await screen.findByText("Token in secret gl-token")).toBeInTheDocument();
    expect(screen.getByText("gl-token")).toBeInTheDocument();
    expect(screen.getByText("oauth2")).toBeInTheDocument();
    expect(screen.getByText("the remote's default")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in your git host" })).not.toBeInTheDocument();
  });

  it("names an https repository's username and a missing secret", async () => {
    renderPage({
      ...ANALYTICS,
      kind: "https",
      url: "https://github.com/acme/analytics.git",
      credential_secret: null,
      credential_username: "x-access-token",
      deploy_keys: [],
    });

    expect(await screen.findByText("x-access-token")).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("says a local folder needs no credential", async () => {
    renderPage({ ...ANALYTICS, kind: "local", url: "/srv/repos/analytics", deploy_keys: [] });

    expect(
      await screen.findByText("Read from the server's disk; no credential."),
    ).toBeInTheDocument();
  });

  it("says when no host key is trusted", async () => {
    renderPage({ ...ANALYTICS, host_keys: undefined, deploy_keys: [] });

    expect(
      await screen.findByText("No host key is trusted yet; deploys over SSH are refused."),
    ).toBeInTheDocument();
    expect(screen.getByText("No deploy key in an environment you can read.")).toBeInTheDocument();
  });

  it("tests access and names the cause and the next step", async () => {
    const { server, user } = renderPage(ANALYTICS, {
      routes: { "POST /api/v1/repository-probes": { status: 202, body: DONE } },
    });

    await user.click(await screen.findByRole("button", { name: /^Environment: dev/ }));
    await user.click(await screen.findByRole("option", { name: /^prod/ }));
    await user.click(screen.getByRole("button", { name: "Test access to analytics" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(DONE.message);
    const queued = server.sent.find((request) => request.path === "/api/v1/repository-probes");
    expect(queued?.body).toEqual({ kind: "access", repository: "analytics", environment: "prod" });
    expect(localStorage.getItem("lakana.repositories.testEnvironment")).toBe("prod");
  });

  it("starts from the environment last tested with, and says when access works", async () => {
    localStorage.setItem("lakana.repositories.testEnvironment", "prod");
    const { user } = renderPage(ANALYTICS, {
      routes: {
        "POST /api/v1/repository-probes": {
          status: 202,
          body: { ...DONE, outcome: "ok", message: "A worker read analytics with prod's key." },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Test access to analytics" }));

    expect(await screen.findByText("A worker read analytics with prod's key.")).toBeInTheDocument();
  });

  it("says when there is no environment to test with", async () => {
    renderPage(ANALYTICS, { routes: { "GET /api/v1/environments": { status: 200, body: [] } } });

    expect(
      await screen.findByText("There is no environment to test with yet."),
    ).toBeInTheDocument();
  });

  it("leaving the page stops the wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const queued = { ...DONE, state: "queued", outcome: null, message: null };
    const { server, user, router } = renderPage(ANALYTICS, {
      routes: {
        "POST /api/v1/repository-probes": { status: 202, body: queued },
        "GET /api/v1/repository-probes/p-1": { status: 200, body: queued },
        "GET /api/v1/repositories": { status: 200, body: [] },
      },
    });
    await user.click(await screen.findByRole("button", { name: "Test access to analytics" }));
    await vi.advanceTimersByTimeAsync(1000);
    const polls = (): number =>
      server.sent.filter((request) => request.path === "/api/v1/repository-probes/p-1").length;
    const before = polls();

    await router.navigate("/settings/repositories");
    await screen.findByRole("heading", { level: 1, name: "Repositories" });
    await vi.advanceTimersByTimeAsync(5000);

    expect(polls()).toBe(before);
  });

  it("a reader sees no write", async () => {
    const { server } = renderPage(ANALYTICS, { reader: true });

    expect(await screen.findByText("SHA256:devkey")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Test access" })).not.toBeInTheDocument();
    for (const name of [/^Edit/, /^Replace key/, /^Delete/, /^Make the missing keys/]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(server.sent.filter((request) => request.method !== "GET")).toEqual([]);
  });

  it("an unknown repository says so", async () => {
    renderPage(ANALYTICS, {
      routes: {
        "GET /api/v1/repositories/analytics": {
          status: 404,
          body: { detail: "no repository named 'analytics'" },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("no repository named");
    expect(screen.getByRole("link", { name: "Back to Repositories" })).toHaveAttribute(
      "href",
      "/settings/repositories",
    );
  });

  it("a failed read is tried again with the focus on the heading", async () => {
    const { server, user } = renderPage(ANALYTICS, {
      routes: {
        "GET /api/v1/repositories/analytics": { status: 500, body: { detail: "down" } },
      },
    });

    const retry = await screen.findByRole("button", { name: "Try again" });
    server.route({ "GET /api/v1/repositories/analytics": { status: 200, body: ANALYTICS } });
    await user.click(retry);

    expect(screen.getByRole("heading", { level: 1, name: "analytics" })).toHaveFocus();
    expect(await screen.findByText("SHA256:devkey")).toBeInTheDocument();
  });

  it("marks a hidden character in the name, the heading and the breadcrumb", async () => {
    renderShell("/settings/repositories/ana%E2%80%AElytics", {
      routes: {
        "GET /api/v1/repositories/ana‮lytics": {
          status: 200,
          body: { ...ANALYTICS, name: "ana‮lytics" },
        },
        "GET /api/v1/environments": { status: 200, body: [anEnvironment("dev")] },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "anaU+202Elytics" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
      "anaU+202Elytics",
    );
    expect(
      await screen.findByRole("button", { name: "Test access to anaU+202Elytics" }),
    ).toBeInTheDocument();
  });
});
