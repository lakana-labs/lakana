import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { anEnvironment, aSecret } from "../test/access";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { SECRETS_ENVIRONMENT_KEY } from "./secretUse";

const PATH = "/settings/access/secrets";

/**
 * The routes of Secrets for kim, who manages the secrets of the environments given.
 *
 * @param manages - Where kim manages secrets.
 * @param more - Routes that differ.
 * @returns The routes.
 */
function secretRoutes(
  manages: readonly string[],
  more: Record<string, FakeRoute> = {},
): Record<string, FakeRoute> {
  return {
    "GET /api/v1/whoami": { status: 200, body: whoami("kim", false, true, false, manages) },
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), { ...anEnvironment("prod"), is_protected: true }],
    },
    "GET /api/v1/environments/dev/secrets": { status: 200, body: [aSecret()] },
    "GET /api/v1/environments/prod/secrets": {
      status: 200,
      body: [aSecret({ id: "s9", name: "prod-hook", environment: "prod", use: null })],
    },
    ...more,
  };
}

/**
 * Renders Secrets for kim.
 *
 * @param manages - Where kim manages secrets.
 * @param path - The address.
 * @param more - Routes that differ.
 * @returns The shell.
 */
function renderSecrets(
  manages: readonly string[],
  path = PATH,
  more: Record<string, FakeRoute> = {},
): ReturnType<typeof renderShell> {
  return renderShell(path, {
    access: areasFor(false),
    me: { name: "kim", admin: false },
    routes: secretRoutes(manages, more),
  });
}

/**
 * The secrets paths read so far.
 *
 * @param shell - The rendered shell.
 * @returns The paths.
 */
function secretReads(shell: ReturnType<typeof renderShell>): string[] {
  return shell.server.sent
    .map((request) => request.path)
    .filter((path) => path.endsWith("/secrets"));
}

afterEach(() => {
  window.localStorage.clear();
});

describe("SecretsPage", () => {
  it("offers only the environments where one manages secrets", async () => {
    const shell = renderSecrets(["dev"]);

    expect(await screen.findByRole("region", { name: "Secrets of dev" })).toBeVisible();
    await shell.user.click(screen.getByRole("button", { name: /^Environment: dev/ }));
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "dev",
    ]);
    expect(secretReads(shell)).toEqual(["/api/v1/environments/dev/secrets"]);
  });

  it("counts the secrets and reads them again on Refresh", async () => {
    const shell = renderSecrets(["dev"]);
    await screen.findByRole("region", { name: "Secrets of dev" });
    expect(await screen.findByText(/^\d+ secrets?$/)).toHaveClass("toolbar-count");

    await shell.user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(secretReads(shell)).toHaveLength(2);
    });
  });

  it("remembers the environment", async () => {
    const shell = renderSecrets(["dev", "prod"]);
    await screen.findByRole("region", { name: "Secrets of dev" });

    await shell.user.click(screen.getByRole("button", { name: /^Environment: dev/ }));
    await shell.user.click(await screen.findByRole("option", { name: /^prod/ }));

    expect(await screen.findByRole("region", { name: "Secrets of prod" })).toBeVisible();
    expect(shell.router.state.location.search).toBe("?environment=prod");
    expect(window.localStorage.getItem(SECRETS_ENVIRONMENT_KEY)).toBe("prod");
    expect(
      screen.getByRole("button", { name: /^Environment: prod, protected/ }),
    ).toBeInTheDocument();
    cleanup();

    renderSecrets(["dev", "prod"]);
    expect(await screen.findByRole("region", { name: "Secrets of prod" })).toBeVisible();
  });

  it("falls back when the remembered environment is gone", async () => {
    window.localStorage.setItem(SECRETS_ENVIRONMENT_KEY, "prod");
    const shell = renderSecrets(["dev"]);

    expect(await screen.findByRole("region", { name: "Secrets of dev" })).toBeVisible();
    expect(screen.queryByText(/You cannot manage the secrets/)).not.toBeInTheDocument();
    expect(secretReads(shell)).toEqual(["/api/v1/environments/dev/secrets"]);
  });

  it("a linked environment one cannot manage reads nothing", async () => {
    const shell = renderSecrets(["dev"], `${PATH}?environment=prod`);

    expect(await screen.findByText("You cannot manage the secrets of prod.")).toBeVisible();
    expect(await screen.findByRole("region", { name: "Secrets of dev" })).toBeVisible();
    expect(secretReads(shell)).toEqual(["/api/v1/environments/dev/secrets"]);
  });

  it("shows each secret's use, creation and rotation", async () => {
    renderSecrets(["dev"], PATH, {
      "GET /api/v1/environments/dev/secrets": {
        status: 200,
        body: [
          aSecret({ rotated_at: "2026-11-15T10:00:00Z" }),
          aSecret({ id: "s2", name: "hook", use: "environment", use_target: null }),
          aSecret({ id: "s3", name: "spare", use: null, use_target: null }),
        ],
      },
    });

    const region = await screen.findByRole("region", { name: "Secrets of dev" });
    const [, corp, hook, spare] = within(region).getAllByRole("row");
    const cells = (row: HTMLElement | undefined): (string | null)[] =>
      within(row as HTMLElement)
        .getAllByRole("cell")
        .map((cell) => cell.textContent);
    expect(cells(corp)[1]).toBe("Package index corp");
    expect(cells(corp)[3]).not.toBe("—");
    expect(cells(hook)[1]).toBe("Storage and webhooks of dev");
    expect(cells(hook)[3]).toBe("—");
    expect(cells(spare)[1]).toBe("no use yet");
  });

  it("a new environment drops the old rows at once", async () => {
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    const shell = renderSecrets(["dev", "prod"], PATH, {
      "GET /api/v1/environments/prod/secrets": () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    await screen.findByText("pypi-corp");

    await shell.user.click(screen.getByRole("button", { name: /^Environment: dev/ }));
    await shell.user.click(await screen.findByRole("option", { name: /^prod/ }));

    expect(screen.queryByText("pypi-corp")).not.toBeInTheDocument();
    answer({ status: 200, body: [] });
    expect(await screen.findByText("No secret in prod yet.")).toBeVisible();
  });

  it("keeps names whole in their own region", async () => {
    const long = "s".repeat(255);
    renderSecrets(["dev"], PATH, {
      "GET /api/v1/environments/dev/secrets": { status: 200, body: [aSecret({ name: long })] },
    });

    const region = await screen.findByRole("region", { name: "Secrets of dev" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("list-scroll", "secrets-scroll");
    expect(within(region).getByText(long)).toHaveClass("row-name");
  });

  it("says so when there is none", async () => {
    renderSecrets(["dev"], PATH, {
      "GET /api/v1/environments/dev/secrets": { status: 200, body: [] },
    });

    expect(await screen.findByText("No secret in dev yet.")).toBeVisible();
    expect(screen.getByText("lakana secret create NAME --env ENV --values-stdin")).toBeVisible();
  });

  it("a failed read keeps the focus", async () => {
    let fail = true;
    const shell = renderSecrets(["dev"], PATH, {
      "GET /api/v1/environments/dev/secrets": () =>
        fail
          ? { status: 500, body: { detail: "The database did not answer." } }
          : { status: 200, body: [] },
    });

    expect(await screen.findByText("The database did not answer.")).toBeVisible();
    fail = false;
    await shell.user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("No secret in dev yet.")).toBeVisible();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Secrets" })).toHaveFocus();
    });
  });

  it("marks each environment protected only where the environments can be read", async () => {
    const shell = renderSecrets(["dev", "prod"], PATH, {
      "GET /api/v1/environments": { status: 403, body: { detail: "No." } },
    });
    await screen.findByRole("region", { name: "Secrets of dev" });

    await shell.user.click(screen.getByRole("button", { name: /^Environment: dev/ }));

    expect(await screen.findByRole("option", { name: "prod" })).toBeVisible();
  });
});
