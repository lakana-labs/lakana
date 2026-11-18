import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { aHeldGrant, anEnvironment, permissionsOf } from "../test/access";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/access/what-i-may-do";
const KIM = { name: "kim", admin: false };

/**
 * The routes of the page for kim.
 *
 * @param routes - What differs: grants, environments, permissions.
 * @param routes.grants - The grants kim holds.
 * @param routes.environments - The environments kim may read.
 * @param routes.permissions - The permissions route.
 * @returns The routes.
 */
function kimRoutes({
  grants = [aHeldGrant()],
  environments = ["dev"],
  permissions = () => ({ status: 200, body: permissionsOf("kim", []) }),
}: {
  grants?: unknown[];
  environments?: string[];
  permissions?: FakeRoute;
}): Record<string, FakeRoute> {
  return {
    "GET /api/v1/principals/kim/grants": { status: 200, body: grants },
    "GET /api/v1/environments": { status: 200, body: environments.map(anEnvironment) },
    "GET /api/v1/principals/kim/permissions": permissions,
  };
}

/**
 * The environment a permissions request names, or "" for the instance.
 *
 * @param path - The request's path.
 * @returns The environment.
 */
function environmentOf(path: string): string {
  return new URLSearchParams(path.split("?")[1] ?? "").get("environment") ?? "";
}

/**
 * Renders the page for kim.
 *
 * @param routes - The routes.
 * @returns The rendered shell.
 */
function renderKim(routes: Record<string, FakeRoute>): ReturnType<typeof renderShell> {
  return renderShell(PATH, { access: areasFor(false), me: KIM, routes });
}

describe("WhatIMayDo", () => {
  it("lists one's grants and through which group", async () => {
    renderKim(
      kimRoutes({
        grants: [
          aHeldGrant({ id: "h-1", group: "analysts" }),
          aHeldGrant({ id: "h-2", role: "VIEWER", environment: "prod" }),
          aHeldGrant({ id: "h-3", role: "VIEWER", kind: "INSTANCE", environment: null }),
        ],
      }),
    );

    const region = await screen.findByRole("region", { name: "The grants you hold" });
    const [, analyst, viewer, instance] = within(region).getAllByRole("row");
    expect(instance).toHaveTextContent("The whole instance");
    expect(analyst).toHaveTextContent("ANALYSTRead tables and run queries");
    expect(analyst).toHaveTextContent("analysts");
    expect(viewer).toHaveTextContent("directly");
    // Kim is not an administrator: the group is named, not a link to a page Kim cannot use.
    expect(within(region).queryByRole("link")).toBeNull();
  });

  it("adds them up per environment", async () => {
    renderKim(
      kimRoutes({
        environments: ["prod", "dev"],
        permissions: (request) => {
          const allowed: Record<string, string[]> = {
            "": [],
            dev: ["READ_ENVIRONMENT", "SUBMIT_QUERY"],
            prod: ["READ_ENVIRONMENT"],
          };
          return {
            status: 200,
            body: permissionsOf("kim", allowed[environmentOf(request.path)] ?? [], ["DEPLOY"]),
          };
        },
      }),
    );

    const addsUp = await screen.findByRole("region", { name: "What they add up to" });
    await waitFor(() => {
      expect(within(addsUp).getAllByRole("heading", { level: 3 })).toHaveLength(2);
    });
    const headings = within(addsUp).getAllByRole("heading", { level: 3 });
    expect(headings.map((heading) => heading.textContent)).toEqual([
      "Environment dev",
      "Environment prod",
    ]);
    const dev = within(addsUp).getByRole("region", { name: /^Environment ?dev$/ });
    expect(
      within(dev)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Read the environment", "Run queries"]);
  });

  it("names the instance's actions, and an action it has no words for as the server does", async () => {
    renderKim(
      kimRoutes({
        permissions: (request) => ({
          status: 200,
          body: permissionsOf(
            "kim",
            environmentOf(request.path) === "" ? ["MANAGE_GROUP", "X_NEW"] : [],
          ),
        }),
      }),
    );
    const instance = await screen.findByRole("region", { name: "On the whole instance" });
    expect(
      within(instance)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Manage groups", "X_NEW"]);
  });

  it("says when nothing adds up", async () => {
    renderKim(kimRoutes({}));
    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
  });

  it("asks about an environment named only by a grant", async () => {
    const { server } = renderKim(
      kimRoutes({ grants: [aHeldGrant({ environment: "staging" })], environments: ["dev"] }),
    );

    await waitFor(() => {
      expect(
        server.sent.map((request) => request.path).filter((path) => path.includes("permissions")),
      ).toContain("/api/v1/principals/kim/permissions?environment=staging");
    });
  });

  it("reads at most four environments at once", async () => {
    let open = 0;
    let most = 0;
    const environments = ["a", "b", "c", "d", "e", "f", "g", "h", "i"];
    const { server } = renderKim(
      kimRoutes({
        environments,
        permissions: async () => {
          open += 1;
          most = Math.max(most, open);
          await new Promise((resolve) => setTimeout(resolve, 5));
          open -= 1;
          return { status: 200, body: permissionsOf("kim", []) };
        },
      }),
    );

    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
    expect(most).toBe(4);
    expect(server.sent.filter((request) => request.path.includes("permissions"))).toHaveLength(11);
  });

  it("keeps the other environments when one cannot be read", async () => {
    let prodTries = 0;
    const { user } = renderKim(
      kimRoutes({
        environments: ["dev", "prod"],
        permissions: (request) => {
          const environment = environmentOf(request.path);
          if (environment === "prod") {
            prodTries += 1;
            if (prodTries === 1) return { status: 500, body: { detail: "The database is down." } };
            return { status: 200, body: permissionsOf("kim", []) };
          }
          return {
            status: 200,
            body: permissionsOf("kim", environment === "dev" ? ["READ_ENVIRONMENT"] : []),
          };
        },
      }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Lakana could not read what you may do in prod: The database is down.",
    );
    const dev = screen.getByRole("region", { name: /^Environment ?dev$/ });
    expect(dev).toHaveTextContent("Read the environment");

    await user.click(screen.getByRole("button", { name: "Try again" }));

    const prod = await screen.findByRole("region", { name: /^Environment ?prod$/ });
    expect(await within(prod).findByText("Nothing yet.")).toBeInTheDocument();
    expect(within(prod).getByRole("heading", { level: 3 })).toHaveFocus();
    expect(screen.getByRole("region", { name: /^Environment ?dev$/ })).toBeInTheDocument();
  });

  it("says so when one holds no grant", async () => {
    const { server } = renderKim(kimRoutes({ grants: [] }));

    expect(
      await screen.findByText(
        "You hold no grant yet. Ask an administrator for a role in an environment.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Your grants" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
    expect(server.sent.filter((request) => request.path.includes("permissions"))).toEqual([]);
  });

  it("reads the grants again after a failure, keeping the focus", async () => {
    let tries = 0;
    const { user } = renderKim({
      ...kimRoutes({}),
      "GET /api/v1/principals/kim/grants": () => {
        tries += 1;
        return tries === 1
          ? { status: 500, body: { detail: "The database is down." } }
          : { status: 200, body: [] };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/You hold no grant yet/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Your grants" })).toHaveFocus();
  });

  it("reads the environments again after a failure, keeping the focus", async () => {
    let tries = 0;
    const { user } = renderKim({
      ...kimRoutes({}),
      "GET /api/v1/environments": () => {
        tries += 1;
        return tries === 1
          ? { status: 500, body: { detail: "The database is down." } }
          : { status: 200, body: [anEnvironment("dev")] };
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Lakana could not read the environments: The database is down.",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "What they add up to" })).toHaveFocus();
  });

  it("says when the instance cannot be read", async () => {
    renderKim(
      kimRoutes({
        permissions: (request) =>
          environmentOf(request.path) === ""
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: permissionsOf("kim", []) },
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Lakana could not read what you may do on the whole instance: The database is down.",
    );
  });

  it("asks only about oneself", async () => {
    const { server } = renderKim(kimRoutes({}));

    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
    const principals = server.sent.filter((request) =>
      request.path.startsWith("/api/v1/principals/"),
    );
    expect(principals.length).toBeGreaterThan(0);
    for (const request of principals) {
      expect(request.path).toMatch(/^\/api\/v1\/principals\/kim\//);
    }
  });
});
