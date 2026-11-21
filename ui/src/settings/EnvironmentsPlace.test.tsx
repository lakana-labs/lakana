import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { areasFor } from "../shell/areas";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";

/** A live environment as the server answers it. */
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

const PROD: EnvironmentOut = {
  ...DEV,
  name: "prod",
  storage_root: "s3://corp-lake/prod",
  storage_secret_ref: "corp-s3",
  is_protected: true,
  retention_days: 90,
};

/** A deleted environment whose files are kept until a week later. */
const DEV_OLD: EnvironmentOut = {
  ...DEV,
  name: "dev-old",
  storage_root: "file:///srv/lakana/dev-old",
  deleted_at: "2026-11-10T09:00:00Z",
  purge_after: "2026-11-17T09:00:00Z",
};

/**
 * How much dev uses.
 *
 * @param complete - Whether every size is measured.
 * @returns The usage.
 */
const usage = (complete = true): unknown => {
  const part = { count: 1, bytes: 0, unmeasured: 0 };
  return {
    environment: "dev",
    tables: part,
    dropped_tables: part,
    artifacts: part,
    code_archives: part,
    total_bytes: 1_200_000_000,
    complete,
  };
};

/**
 * The lists' answers: the live ones, and the deleted ones for `?deleted=true`.
 *
 * @param live - The live environments.
 * @param deleted - The deleted environments.
 * @returns The route.
 */
const lists = (
  live: readonly EnvironmentOut[],
  deleted: readonly EnvironmentOut[] = [],
): Record<string, FakeRoute> => ({
  "GET /api/v1/environments": (request) => ({
    status: 200,
    body: request.path.endsWith("?deleted=true") ? deleted : live,
  }),
});

/**
 * The routes of mara, who manages dev and dev-old and reads prod, and manages nothing on the
 * instance.
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
          managedEnvironments: ["dev", "dev-old"],
        }),
      },
      ...routes,
    },
  };
}

/**
 * The routes of kim, who reads environments and manages none.
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
 * The cells of a region's rows, below its header.
 *
 * @param region - The region.
 * @returns Each row's cells' text.
 */
const cells = (region: HTMLElement): (string | null)[][] =>
  within(region)
    .getAllByRole("row")
    .slice(1)
    .map((row) => [...(row as HTMLTableRowElement).cells].map((cell) => cell.textContent));

describe("EnvironmentsPlace", () => {
  it("lists name, protected mark, storage and retention", async () => {
    renderShell("/settings/environments", asReader(lists([DEV, PROD])));

    const region = await screen.findByRole("region", { name: "Environments" });
    expect(cells(region)).toEqual([
      ["dev", "", "Local folder · file:///srv/lakana/dev", "keeps tables", "›"],
      ["prod", "● protected", "S3 · s3://corp-lake/prod", "tables expire after 90 days", "›"],
    ]);
    expect(within(region).getByText("protected")).toHaveClass("env-protected");
    expect(within(region).getByRole("link", { name: "prod" })).toHaveAttribute(
      "href",
      "/settings/environments/prod",
    );
    expect(screen.getByRole("link", { name: "Environments" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("usage only where one manages", async () => {
    const { server } = renderShell(
      "/settings/environments",
      asMara({
        ...lists([DEV, PROD]),
        "GET /api/v1/environments/dev/usage": { status: 200, body: usage() },
      }),
    );

    const region = await screen.findByRole("region", { name: "Environments" });
    expect(await within(region).findByText("1.2 GB")).toBeInTheDocument();
    expect(cells(region).map((row) => row[4])).toEqual(["1.2 GB", "—"]);
    expect(server.sent.some((request) => request.path.includes("/prod/usage"))).toBe(false);
    cleanup();

    renderShell(
      "/settings/environments",
      asMara({
        ...lists([DEV]),
        "GET /api/v1/environments/dev/usage": { status: 200, body: usage(false) },
      }),
    );
    expect(await screen.findByText("at least 1.2 GB")).toBeInTheDocument();
  });

  it("stops the usage reads still in flight when the list is left", async () => {
    // One answered after the environment is deleted is a 404 the browser logs.
    let usage: AbortSignal | undefined;
    const { server, user } = renderShell(
      "/settings/environments",
      asMara({
        ...lists([DEV]),
        "GET /api/v1/environments/dev/usage": (request) => {
          usage = request.signal;
          return new Promise<never>(() => undefined);
        },
      }),
    );
    await waitFor(() => {
      expect(server.sent.some((request) => request.path.endsWith("/dev/usage"))).toBe(true);
    });
    expect(usage?.aborted).toBe(false);

    await user.click(await screen.findByRole("link", { name: "dev" }));

    expect(usage?.aborted).toBe(true);
  });

  it("asks for no more usage once the list is left", async () => {
    // More environments than reads at once: the ones still waiting are never asked for after the
    // person opens one.
    const names = ["e1", "e2", "e3", "e4", "e5", "e6"];
    const many = names.map((name) => ({ ...DEV, name }));
    const base = asMara(lists(many));
    const { server, user } = renderShell("/settings/environments", {
      ...base,
      routes: {
        ...base?.routes,
        "GET /api/v1/whoami": {
          status: 200,
          body: whoami("mara", false, true, false, [], false, {
            readsEnvironments: true,
            managedEnvironments: names,
          }),
        },
        ...Object.fromEntries(
          names.map((name) => [
            `GET /api/v1/environments/${name}/usage`,
            (request: { readonly signal?: AbortSignal }): Promise<never> =>
              new Promise<never>((_, reject) => {
                request.signal?.addEventListener("abort", () => {
                  reject(new Error("aborted"));
                });
              }),
          ]),
        ),
      },
    });
    const asked = (): string[] =>
      server.sent
        .filter((request) => request.path.endsWith("/usage"))
        .map((request) => request.path);
    await waitFor(() => {
      expect(asked()).toHaveLength(4);
    });

    await user.click(await screen.findByRole("link", { name: "e1" }));

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Environments" })).not.toBeInTheDocument();
    });
    expect(asked().filter((path) => !path.startsWith("/api/v1/environments/e1/"))).toHaveLength(3);
    expect(asked()).not.toContain("/api/v1/environments/e5/usage");
    expect(asked()).not.toContain("/api/v1/environments/e6/usage");
  });

  it("a failed usage read does not fail the list", async () => {
    renderShell(
      "/settings/environments",
      asMara({
        ...lists([DEV, PROD]),
        "GET /api/v1/environments/dev/usage": { status: 500, body: { detail: "down" } },
      }),
    );

    expect(await screen.findByText("could not be read")).toHaveClass("environment-usage-failed");
    expect(screen.getByRole("link", { name: "prod" })).toBeInTheDocument();
  });

  it("lists the deleted environments one manages", async () => {
    renderShell(
      "/settings/environments",
      asMara({
        ...lists([DEV], [DEV_OLD, { ...DEV_OLD, name: "qa-old" }]),
        "GET /api/v1/environments/dev/usage": { status: 200, body: usage() },
      }),
    );

    expect(
      await screen.findByRole("heading", { level: 2, name: "Deleted environments" }),
    ).toHaveAttribute("id", "deleted");
    const region = await screen.findByRole("region", { name: "Deleted environments" });
    const [row] = cells(region);
    expect(cells(region)).toHaveLength(1);
    expect(row?.[0]).toBe("dev-old");
    expect(row?.[1]).toMatch(/2026/);
    expect(row?.[2]).toMatch(/2026/);
    expect(within(region).queryByRole("link")).not.toBeInTheDocument();
  });

  it("a failed read of the deleted ones keeps the focus on their heading", async () => {
    const { server, user } = renderShell(
      "/settings/environments",
      asMara({
        ...lists([]),
        "GET /api/v1/environments": (request) =>
          request.path.endsWith("?deleted=true")
            ? { status: 500, body: { detail: "down" } }
            : { status: 200, body: [] },
      }),
    );

    const retry = await screen.findByRole("button", { name: "Try again" });
    server.route(lists([], [DEV_OLD]));
    await user.click(retry);

    expect(screen.getByRole("heading", { level: 2, name: "Deleted environments" })).toHaveFocus();
    expect(await screen.findByText("dev-old")).toBeInTheDocument();
  });

  it("shows no deleted section to a reader, nor to a manager without deleted ones", async () => {
    const { server } = renderShell("/settings/environments", asReader(lists([DEV])));
    await screen.findByRole("link", { name: "dev" });
    expect(server.sent.some((request) => request.path.endsWith("?deleted=true"))).toBe(false);
    cleanup();

    renderShell("/settings/environments", asMara(lists([], [])));
    await screen.findByText(/You can read no environment now/);
    expect(screen.queryByText("Deleted environments")).not.toBeInTheDocument();
  });

  it("Create environment is the instance manager's", async () => {
    renderShell("/settings/environments", { routes: lists([DEV]) });
    expect(await screen.findByRole("button", { name: "Create environment" })).toBeInTheDocument();
    cleanup();

    renderShell("/settings/environments", asMara(lists([DEV])));
    await screen.findByRole("link", { name: "dev" });
    expect(screen.queryByRole("button", { name: "Create environment" })).not.toBeInTheDocument();
  });

  it("says so when there is none", async () => {
    renderShell("/settings/environments", { routes: lists([]) });
    expect(await screen.findByText("No environment yet.")).toBeInTheDocument();
    cleanup();

    renderShell("/settings/environments", asReader(lists([])));
    expect(
      await screen.findByText(
        "You can read no environment now. Ask an administrator for a role in one.",
      ),
    ).toBeInTheDocument();
  });

  it("marks a hidden character in an environment's name, and keeps names whole in the region", async () => {
    renderShell(
      "/settings/environments",
      asReader(lists([{ ...DEV, name: "de‮v", storage_root: "file:///srv/de‮v" }])),
    );

    const link = await screen.findByRole("link", { name: "deU+202Ev" });
    expect(link).toHaveTextContent("deU+202Ev");
    expect(screen.getByText("Local folder · file:///srv/deU+202Ev")).toHaveClass(
      "environment-storage",
    );
    expect(screen.getByRole("region", { name: "Environments" })).toHaveClass("environments-scroll");
  });

  it("names a root Lakana cannot reach", async () => {
    renderShell("/settings/environments", asReader(lists([{ ...DEV, storage_root: "hdfs://x" }])));

    expect(
      await screen.findByText("hdfs://x (Lakana cannot reach this storage)"),
    ).toBeInTheDocument();
  });

  it("a failed read keeps the focus", async () => {
    const { server, user } = renderShell("/settings/environments", {
      routes: { "GET /api/v1/environments": { status: 500, body: { detail: "down" } } },
    });

    const retry = await screen.findAllByRole("button", { name: "Try again" });
    server.route(lists([DEV]));
    await user.click(retry[0] as HTMLElement);

    expect(screen.getByRole("heading", { level: 1, name: "Environments" })).toHaveFocus();
    expect(await screen.findByRole("link", { name: "dev" })).toBeInTheDocument();
  });

  it("opens an environment's page with its breadcrumb", async () => {
    const { user } = renderShell("/settings/environments", asReader(lists([DEV])));

    await user.click(await screen.findByRole("link", { name: "dev" }));

    expect(await screen.findByRole("heading", { level: 1, name: "dev" })).toHaveFocus();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Environments" })).toHaveAttribute(
      "href",
      "/settings/environments",
    );
  });

  it("answers an address below a page that does not exist", async () => {
    renderShell("/settings/environments/dev/nothing", asReader(lists([DEV])));
    expect(await screen.findByText("This page does not exist")).toBeInTheDocument();
  });
});
