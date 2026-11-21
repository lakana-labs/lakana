import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { entryPath, SETTINGS_PLACES } from "./places";

const ADMIN = {
  admin: true,
  noAccessYet: false,
  secretEnvironments: ["dev", "prod"],
  managePolicy: true,
  readsEnvironments: true,
  instanceActions: new Set(["MANAGE_REPOSITORY"]),
  managedEnvironments: [],
};

/** A repository as the server answers it, read over SSH with the deploy keys of dev and prod. */
const ANALYTICS = {
  id: "r-1",
  name: "analytics",
  kind: "ssh",
  url: "git@github.com:acme/analytics.git",
  credential_secret: "analytics-deploy-key",
  credential_username: null,
  ssh_known_hosts: "github.com ssh-ed25519 AAAA\n",
  default_ref: "main",
  created_by: null,
  created_at: "2026-11-01T09:00:00Z",
  updated_at: "2026-11-02T09:00:00Z",
  deploy_keys: [
    { environment: "dev", public_key: "ssh-ed25519 AAAA dev", fingerprint: "SHA256:dev" },
    { environment: "prod", public_key: "ssh-ed25519 AAAA prod", fingerprint: "SHA256:prod" },
  ],
  host_keys: [],
};

const DBT = {
  ...ANALYTICS,
  id: "r-2",
  name: "dbt-models",
  kind: "https",
  url: "https://github.com/acme/dbt-models.git",
  credential_secret: "gh-token",
  ssh_known_hosts: null,
  default_ref: null,
  deploy_keys: [],
};

const LOCAL = {
  ...DBT,
  id: "r-3",
  name: "local-dev",
  kind: "local",
  url: "/srv/lakana/repos/local-dev",
  credential_secret: null,
};

/**
 * The routes of a person who reads dev and manages no repository.
 *
 * @param routes - More routes.
 * @returns The options of `renderShell`.
 */
function asReader(routes: Record<string, FakeRoute>): Parameters<typeof renderShell>[1] {
  return {
    access: areasFor(false),
    me: { name: "kim", admin: false },
    routes: {
      "GET /api/v1/whoami": {
        status: 200,
        body: whoami("kim", false, true, false, [], false, { readsEnvironments: true }),
      },
      ...routes,
    },
  };
}

/**
 * The list's answer.
 *
 * @param body - The repositories.
 * @returns The route.
 */
const listed = (body: unknown): Record<string, FakeRoute> => ({
  "GET /api/v1/repositories": { status: 200, body },
});

describe("RepositoriesPlace", () => {
  it("shows the heading, the list of places, the intro and the way to connect", async () => {
    renderShell("/settings/repositories", { routes: listed([]) });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Repositories" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Status" })).toHaveAttribute(
      "href",
      "/settings/status",
    );
    expect(screen.getByRole("link", { name: "Sign-in" })).toHaveAttribute(
      "href",
      "/settings/sign-in",
    );
    expect(screen.getByText(/only ever reads from them/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect a repository" })).toHaveAttribute(
      "href",
      "/settings/repositories/new",
    );
  });

  it("leads to Connect a repository, whose breadcrumb names Repositories", async () => {
    const { user } = renderShell("/settings/repositories", { routes: listed([]) });

    await user.click(await screen.findByRole("link", { name: "Connect a repository" }));

    const crumbs = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "href",
      "/settings/repositories",
    );
  });

  // A group (Access) has no page: its address opens its first place, which is marked current.
  it.each(SETTINGS_PLACES.map((place) => [place.path, entryPath(place, ADMIN)]))(
    "lists the place %s, marks %s current, and the place renders",
    async (path, entry) => {
      renderShell(`/settings/${path}`, { routes: listed([]) });

      const places = await screen.findByRole("navigation", { name: "Settings places" });
      expect(await within(places).findByRole("link", { current: "page" })).toHaveAttribute(
        "href",
        entry,
      );
      expect(screen.queryByText("This page does not exist")).not.toBeInTheDocument();
    },
  );

  it("lists the repositories with URL, kind, credential and ref", async () => {
    renderShell("/settings/repositories", { routes: listed([ANALYTICS, DBT, LOCAL]) });

    const region = await screen.findByRole("region", { name: "Repositories" });
    const rows = within(region)
      .getAllByRole("row")
      .slice(1)
      .map((row) => [...(row as HTMLTableRowElement).cells].map((cell) => cell.textContent));
    expect(rows).toEqual([
      [
        "analytics",
        "git@github.com:acme/analytics.git",
        "SSH",
        "Deploy key in 2 environments",
        "main",
        "›",
      ],
      [
        "dbt-models",
        "https://github.com/acme/dbt-models.git",
        "HTTPS",
        "Token in secret gh-token",
        "the remote's default",
        "›",
      ],
      [
        "local-dev",
        "/srv/lakana/repos/local-dev",
        "Local folder",
        "None",
        "the remote's default",
        "›",
      ],
    ]);
    expect(within(region).getByRole("link", { name: "dbt-models" })).toHaveAttribute(
      "href",
      "/settings/repositories/dbt-models",
    );
  });

  it("says a repository has no credential in the warning colour", async () => {
    renderShell("/settings/repositories", {
      routes: listed([
        { ...DBT, credential_secret: null },
        { ...ANALYTICS, deploy_keys: [] },
      ]),
    });

    const missing = await screen.findByText("No credential");
    expect(missing).toHaveClass("repository-uncredentialed");
    expect(screen.getByText("SSH key in secret analytics-deploy-key")).not.toHaveClass(
      "repository-uncredentialed",
    );
  });

  it("Connect is a manager's", async () => {
    renderShell("/settings/repositories", { routes: listed([]) });
    expect(await screen.findByRole("link", { name: "Connect a repository" })).toBeInTheDocument();
    cleanup();

    renderShell("/settings/repositories", asReader(listed([])));
    await screen.findByText(/No repository is connected yet/);
    expect(screen.queryByRole("link", { name: "Connect a repository" })).not.toBeInTheDocument();
  });

  it("refuses Connect to a reader and loads nothing of it", async () => {
    const { server } = renderShell("/settings/repositories/new", asReader({}));

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Connect a repository" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.includes("/repository-probes"))).toEqual(
      [],
    );
  });

  it("says so when there is none", async () => {
    renderShell("/settings/repositories", { routes: listed([]) });
    expect(await screen.findByText("No repository yet.")).toBeInTheDocument();
    cleanup();

    renderShell("/settings/repositories", asReader(listed([])));
    expect(
      await screen.findByText(
        "No repository is connected yet. Someone who manages repositories connects them.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana repo list")).toBeInTheDocument();
  });

  it("a repository named new opens its page", async () => {
    const { user } = renderShell("/settings/repositories", {
      routes: {
        ...listed([{ ...ANALYTICS, name: "new" }]),
        "GET /api/v1/repositories/new": { status: 200, body: { ...ANALYTICS, name: "new" } },
      },
    });

    const link = await screen.findByRole("link", { name: "new" });
    expect(link).toHaveAttribute("href", "/settings/repositories/new/about");
    await user.click(link);

    expect(await screen.findByRole("heading", { level: 1, name: "new" })).toHaveFocus();
    expect(screen.queryByText(/Connect a repository/, { selector: "h1" })).not.toBeInTheDocument();
  });

  it("marks a hidden character in a repository's name everywhere", async () => {
    renderShell("/settings/repositories", {
      routes: listed([{ ...ANALYTICS, name: "ana\u202elytics" }]),
    });

    const link = await screen.findByRole("link", { name: "anaU+202Elytics" });
    expect(link).toHaveTextContent("anaU+202Elytics");
  });

  it("keeps the list in its own region", async () => {
    const url = `https://git.example.com/${"data-platform/".repeat(23)}x.git`;
    renderShell("/settings/repositories", { routes: listed([{ ...DBT, url }]) });

    const region = await screen.findByRole("region", { name: "Repositories" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(url.length).toBeGreaterThanOrEqual(300);
    expect(within(region).getByText(url).closest("td")).toHaveClass("repository-url");
  });

  it("a failed read keeps the focus", async () => {
    const { server, user } = renderShell("/settings/repositories", {
      routes: { "GET /api/v1/repositories": { status: 500, body: { detail: "down" } } },
    });

    const retry = await screen.findByRole("button", { name: "Try again" });
    server.route(listed([ANALYTICS]));
    await user.click(retry);

    expect(screen.getByRole("heading", { level: 1, name: "Repositories" })).toHaveFocus();
    expect(await screen.findByRole("link", { name: "analytics" })).toBeInTheDocument();
  });

  it("counts the repositories and reads them again on Refresh", async () => {
    const { server, user } = renderShell("/settings/repositories", {
      routes: listed([ANALYTICS, DBT, LOCAL]),
    });
    expect(await screen.findByText("3 repositories")).toBeInTheDocument();
    const reads = (): number =>
      server.sent.filter(
        (request) => request.method === "GET" && request.path.startsWith("/api/v1/repositories"),
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
