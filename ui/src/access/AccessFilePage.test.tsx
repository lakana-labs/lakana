import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AccessOverrideOut, ManagedScopeOut } from "../api/accessFile";
import { renderShell } from "../test/render";
import { scopeState } from "./AccessFilePage";

const PATH = "/settings/access/access-file";

/**
 * A managed scope.
 *
 * @param over - What differs from the instance managed by access.yaml, applied by admin.
 * @returns The scope.
 */
function aScope(over: Partial<ManagedScopeOut> = {}): ManagedScopeOut {
  return {
    scope: "instance",
    file_name: "access.yaml",
    file_sha256: "3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c",
    source_commit: "0123456789abcdef0123",
    applied_by: "admin",
    applied_at: "2026-11-17T09:00:00Z",
    ...over,
  };
}

/**
 * A change made by hand.
 *
 * @param over - What differs from bob's grant in prod.
 * @returns The override.
 */
function anOverride(over: Partial<AccessOverrideOut> = {}): AccessOverrideOut {
  return {
    scope: "prod",
    change: "grant VIEWER on prod to user:bob",
    reason: "incident 42",
    by: "admin",
    at: "2026-11-18T09:00:00Z",
    ...over,
  };
}

/**
 * Renders Access file with a status.
 *
 * @param scopes - The managed scopes.
 * @param overrides - The open overrides.
 * @returns The rendered shell.
 */
function renderStatus(
  scopes: readonly ManagedScopeOut[],
  overrides: readonly AccessOverrideOut[] = [],
): ReturnType<typeof renderShell> {
  return renderShell(PATH, {
    routes: { "GET /api/v1/access/status": { status: 200, body: { scopes, overrides } } },
  });
}

describe("AccessFilePage", () => {
  it("lists each managed scope with its file and last apply", async () => {
    renderStatus([aScope(), aScope({ scope: "prod", applied_by: null, source_commit: null })]);

    const region = await screen.findByRole("region", { name: "Managed scopes" });
    const [, instance, prod] = within(region).getAllByRole("row");
    expect(instance).toHaveTextContent("The whole instance");
    expect(instance).toHaveTextContent("access.yamlsha256 3f2a9b8c7d6e, commit 0123456789ab");
    expect(instance).toHaveTextContent("by admin");
    expect(prod).toHaveTextContent("prod");
    expect(prod).toHaveTextContent("by a deleted account");
    expect(prod).not.toHaveTextContent("commit");
  });

  it("says which scopes were changed by hand", async () => {
    renderStatus(
      [aScope(), aScope({ scope: "prod" })],
      [anOverride(), anOverride({ change: "grant ANALYST on prod to user:kim", by: null })],
    );

    const scopes = await screen.findByRole("region", { name: "Managed scopes" });
    const [, instance, prod] = within(scopes).getAllByRole("row");
    expect(instance).toHaveTextContent("In sync");
    expect(prod).toHaveTextContent("Changed by hand (2)");
    expect(instance?.querySelector(".status-glyph-succeeded")).not.toBeNull();
    expect(prod?.querySelector(".status-glyph-warning")).not.toBeNull();
    const changes = screen.getByRole("region", { name: "Changes by hand" });
    const [, first, second] = within(changes).getAllByRole("row");
    expect(first).toHaveTextContent("grant VIEWER on prod to user:bobincident 42admin");
    expect(second).toHaveTextContent("a deleted account");
    expect(
      screen.getByText("The next apply reverts each change the file does not list."),
    ).toBeInTheDocument();
  });

  it("marks what people typed", async () => {
    renderStatus(
      [aScope({ file_name: "acc​ess.yaml", applied_by: "ad‮min" })],
      [anOverride({ scope: "instance", reason: "inci‮dent", by: "ad‮min" })],
    );

    const scopes = await screen.findByRole("region", { name: "Managed scopes" });
    expect(scopes).toHaveTextContent("accU+200Bess.yaml");
    expect(scopes).toHaveTextContent("by adU+202Emin");
    const changes = screen.getByRole("region", { name: "Changes by hand" });
    expect(changes).toHaveTextContent("inciU+202Edent");
    expect(changes.textContent).not.toContain("‮");
    expect(document.body).toHaveTextContent("lakana access plan accU+200Bess.yaml");
  });

  it("says so when no file manages anything", async () => {
    renderStatus([]);

    expect(
      await screen.findByText(
        "No access file manages any scope yet. Access is changed here and from the command line.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Changes by hand" })).toBeNull();
    expect(screen.getByText("lakana access plan access.yaml")).toBeInTheDocument();
    expect(
      screen.getByText("lakana access apply access.yaml --source-commit COMMIT"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("lakana access export --scope instance -o access.yaml"),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana access plan").tagName).toBe("CODE");
  });

  it("names the managed file in the commands", async () => {
    renderStatus([
      aScope({ scope: "dev", file_name: "dev.yaml" }),
      aScope({ file_name: "lakana-access.yaml" }),
    ]);

    expect(await screen.findByText("lakana access plan lakana-access.yaml")).toBeInTheDocument();
  });

  it("names an environment's file when the instance is not managed", async () => {
    renderStatus([aScope({ scope: "dev", file_name: "dev.yaml" })]);

    expect(await screen.findByText("lakana access plan dev.yaml")).toBeInTheDocument();
  });

  it("a failed read keeps the focus", async () => {
    let fail = true;
    const { user } = renderShell(PATH, {
      routes: {
        "GET /api/v1/access/status": () =>
          fail
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: { scopes: [], overrides: [] } },
      },
    });

    expect(await screen.findByText(/The database is down\./)).toBeInTheDocument();
    expect(screen.getByText("lakana access plan access.yaml")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/No access file manages any scope yet/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Access file" })).toHaveFocus();
  });

  it("asks only what an administrator may", async () => {
    const { server } = renderStatus([]);

    await screen.findByText(/No access file manages any scope yet/);
    await waitFor(() => {
      expect(
        server.sent
          .filter((request) => request.path.startsWith("/api/v1/access/"))
          .map((request) => request.path),
      ).toEqual(["/api/v1/access/status"]);
    });
  });
});

describe("scopeState", () => {
  it("counts only the scope's own open overrides", () => {
    expect(scopeState(aScope({ scope: "prod" }), [anOverride({ scope: "dev" })])).toEqual({
      kind: "succeeded",
      count: 0,
    });
    expect(scopeState(aScope({ scope: "prod" }), [anOverride(), anOverride()])).toEqual({
      kind: "warning",
      count: 2,
    });
  });
});
