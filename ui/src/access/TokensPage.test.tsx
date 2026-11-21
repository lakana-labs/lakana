import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TaskTokenOut, TokenOut } from "../api/tokens";
import { areasFor } from "../shell/areas";
import { anEnvironment } from "../test/access";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/access/tokens";

/**
 * A personal token.
 *
 * @param over - What differs from maria's live token laptop.
 * @returns The token.
 */
function aToken(over: Partial<TokenOut> = {}): TokenOut {
  return {
    key_id: "k1",
    name: "laptop",
    owner: "maria",
    created_at: "2026-11-10T09:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    ...over,
  };
}

/**
 * A task token.
 *
 * @param over - What differs from a token of the job sales/nightly's run in dev.
 * @returns The token.
 */
function aTaskToken(over: Partial<TaskTokenOut> = {}): TaskTokenOut {
  return {
    key_id: "t1",
    principal: "ci-bot",
    environment: "dev",
    asset_scope: ["gold.*"],
    issued_at: "2026-11-18T09:00:00Z",
    expires_at: "2026-11-18T10:00:00Z",
    run_id: "r-1",
    job: "sales/nightly",
    asset: null,
    ...over,
  };
}

/**
 * The routes of Tokens: the two lists and the environments.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function tokenRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    "GET /api/v1/tokens": {
      status: 200,
      body: [
        aToken(),
        aToken({
          key_id: "k9",
          name: "export",
          last_used_at: "2026-11-17T09:00:00Z",
          expires_at: "2027-01-01T00:00:00Z",
        }),
      ],
    },
    "GET /api/v1/task-tokens": {
      status: 200,
      body: [aTaskToken(), aTaskToken({ key_id: "t2", run_id: null, job: null })],
    },
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    ...more,
  };
}

/**
 * The paths of the requests sent so far that start with a prefix.
 *
 * @param shell - The rendered shell.
 * @param prefix - The prefix.
 * @returns The paths.
 */
function sentTo(shell: ReturnType<typeof renderShell>, prefix: string): string[] {
  return shell.server.sent.map((request) => request.path).filter((path) => path.startsWith(prefix));
}

describe("TokensPage", () => {
  it("lists everyone's live tokens and the task tokens", async () => {
    renderShell(PATH, { routes: tokenRoutes() });

    const personal = await screen.findByRole("region", { name: "Live personal tokens" });
    const [, row, used] = within(personal).getAllByRole("row");
    expect(used).not.toHaveTextContent("never");
    const cells = within(row as HTMLElement).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toEqual([
      expect.stringContaining("maria"),
      "laptop",
      expect.any(String),
      "never",
      "never",
      "Revoke",
    ]);
    expect(within(cells[0] as HTMLElement).getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
    const task = await screen.findByRole("region", { name: "Live task tokens" });
    const [, job, byHand] = within(task).getAllByRole("row");
    expect(within(job as HTMLElement).getByText("dev")).toBeVisible();
    expect(within(job as HTMLElement).getByRole("link", { name: "sales/nightly" })).toHaveAttribute(
      "href",
      "/jobs/dev/runs/r-1",
    );
    expect(byHand).toHaveTextContent("minted by hand");
    expect(
      within(task).getAllByRole("button", { name: /^Revoke the task token of ci-bot in dev/ }),
    ).toHaveLength(2);
  });

  it("names an asset, linked to its run only when it has one", async () => {
    renderShell(PATH, {
      routes: tokenRoutes({
        "GET /api/v1/task-tokens": {
          status: 200,
          body: [
            aTaskToken({ job: null, asset: "gold.sales" }),
            aTaskToken({ key_id: "t2", run_id: null, job: null, asset: "gold.daily" }),
          ],
        },
      }),
    });

    const task = await screen.findByRole("region", { name: "Live task tokens" });
    expect(within(task).getByRole("link", { name: "gold.sales" })).toHaveAttribute(
      "href",
      "/jobs/dev/runs/r-1",
    );
    expect(within(task).getByText("gold.daily").closest("a")).toBeNull();
    expect(
      within(task).getByRole("button", {
        name: "Revoke the task token of ci-bot in dev, the asset gold.daily",
      }),
    ).toBeVisible();
  });

  it("filters each list by its own field, in the address", async () => {
    const shell = renderShell(PATH, {
      routes: tokenRoutes({
        "GET /api/v1/tokens": (request) => ({
          status: 200,
          body: request.path.includes("owner=maria") ? [aToken({ name: "ci" })] : [aToken()],
        }),
      }),
    });
    await screen.findByRole("region", { name: "Live personal tokens" });

    await shell.user.type(screen.getByRole("textbox", { name: "Owner" }), "maria{Enter}");

    await waitFor(() => {
      expect(shell.router.state.location.search).toBe("?owner=maria");
    });
    expect(screen.queryByText("laptop")).not.toBeInTheDocument();
    expect(await screen.findByText("ci")).toBeVisible();
    expect(sentTo(shell, "/api/v1/tokens")).toEqual([
      "/api/v1/tokens?limit=50",
      "/api/v1/tokens?owner=maria&limit=50",
    ]);

    await shell.user.click(screen.getByRole("button", { name: /^Environment: All environments/ }));
    await shell.user.click(await screen.findByRole("option", { name: "dev" }));

    await waitFor(() => {
      expect(shell.router.state.location.search).toBe("?owner=maria&environment=dev");
    });
    await waitFor(() => {
      expect(sentTo(shell, "/api/v1/task-tokens").at(-1)).toBe(
        "/api/v1/task-tokens?environment=dev&limit=50",
      );
    });
    expect(sentTo(shell, "/api/v1/tokens")).toHaveLength(2);
    expect(screen.getByRole("textbox", { name: "Owner" })).toHaveValue("maria");
  });

  it("clears the owner when the field is emptied and left", async () => {
    const shell = renderShell(`${PATH}?owner=maria`, { routes: tokenRoutes() });
    const field = await screen.findByRole("textbox", { name: "Owner" });
    expect(field).toHaveValue("maria");

    await shell.user.clear(field);
    await shell.user.tab();

    await waitFor(() => {
      expect(shell.router.state.location.search).toBe("");
    });
    await shell.user.click(field);
    await shell.user.tab();
    expect(sentTo(shell, "/api/v1/tokens")).toEqual([
      "/api/v1/tokens?owner=maria&limit=50",
      "/api/v1/tokens?limit=50",
    ]);
  });

  it("says when no such owner exists", async () => {
    renderShell(`${PATH}?owner=nobody`, {
      routes: tokenRoutes({
        "GET /api/v1/tokens": {
          status: 404,
          body: { detail: "No principal named 'nobody'.", type: "NotFoundError" },
        },
      }),
    });

    expect(await screen.findByText("No principal named 'nobody'.")).toBeVisible();
    expect(await screen.findByRole("region", { name: "Live task tokens" })).toBeVisible();
  });

  it("says when a list is empty", async () => {
    renderShell(`${PATH}?owner=maria`, {
      routes: tokenRoutes({
        "GET /api/v1/tokens": { status: 200, body: [] },
        "GET /api/v1/task-tokens": { status: 200, body: [] },
      }),
    });

    expect(await screen.findByText("maria has no live token.")).toBeVisible();
    expect(await screen.findByText("No live task token.")).toBeVisible();
  });

  it("says so when nobody has a live token", async () => {
    renderShell(PATH, { routes: tokenRoutes({ "GET /api/v1/tokens": { status: 200, body: [] } }) });

    expect(await screen.findByText("No live personal token.")).toBeVisible();
  });

  it("revokes without a touch, and reads the task tokens again", async () => {
    let revoked = false;
    const shell = renderShell(PATH, {
      routes: tokenRoutes({
        "GET /api/v1/tokens": () => ({ status: 200, body: revoked ? [] : [aToken()] }),
        "DELETE /api/v1/principals/maria/tokens/k1": () => {
          revoked = true;
          return { status: 204 };
        },
      }),
    });

    await shell.user.click(await screen.findByRole("button", { name: "Revoke laptop of maria" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke this token?" });
    expect(dialog).toHaveTextContent("The token laptop of maria.");
    expect(dialog).toHaveTextContent("Scripts that use it stop at once.");
    expect(dialog).toHaveTextContent("Task tokens minted with it stop working too.");
    const tasksBefore = sentTo(shell, "/api/v1/task-tokens").length;
    await shell.user.click(within(dialog).getByRole("button", { name: "Revoke the token" }));

    expect(await screen.findByText("No live personal token.")).toBeVisible();
    await waitFor(() => {
      // The list's count is the panel's heading.
      expect(screen.getByRole("heading", { level: 2, name: "0 personal tokens" })).toHaveFocus();
    });
    const index = shell.server.sent.findIndex((request) => request.method === "DELETE");
    expect(shell.server.sent[index]?.path).toBe("/api/v1/principals/maria/tokens/k1");
    expect(
      shell.server.sent
        .slice(0, index)
        .some((request) => request.path === "/api/v1/sign-in/session"),
    ).toBe(false);
    await waitFor(() => {
      expect(sentTo(shell, "/api/v1/task-tokens").length).toBeGreaterThan(tasksBefore);
    });
  });

  it("a revoke reads the list again from the first page", async () => {
    let revoked = false;
    const shell = renderShell(PATH, {
      routes: tokenRoutes({
        "GET /api/v1/tokens": (request) =>
          request.path.includes("cursor=c2")
            ? { status: 200, body: [aToken({ key_id: "k2", name: "ci" })] }
            : {
                status: 200,
                body: revoked ? [] : [aToken()],
                headers: { "X-Next-Cursor": "c2" },
              },
        "DELETE /api/v1/principals/maria/tokens/k2": () => {
          revoked = true;
          return { status: 204 };
        },
      }),
    });
    await shell.user.click(await screen.findByRole("button", { name: "Show more" }));
    await shell.user.click(await screen.findByRole("button", { name: "Revoke ci of maria" }));
    await shell.user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke the token" }),
    );

    await waitFor(() => {
      expect(sentTo(shell, "/api/v1/tokens")).toEqual([
        "/api/v1/tokens?limit=50",
        "/api/v1/tokens?limit=50&cursor=c2",
        "/api/v1/tokens?limit=50",
      ]);
    });
  });

  it("the focus goes to the section when its last token goes", async () => {
    let revoked = false;
    const shell = renderShell(PATH, {
      routes: tokenRoutes({
        "GET /api/v1/task-tokens": () => ({ status: 200, body: revoked ? [] : [aTaskToken()] }),
        "DELETE /api/v1/task-tokens/t1": () => {
          revoked = true;
          return { status: 204 };
        },
      }),
    });

    await shell.user.click(
      await screen.findByRole("button", {
        name: "Revoke the task token of ci-bot in dev, the job sales/nightly",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Revoke this token?" });
    expect(dialog).toHaveTextContent("The task token of ci-bot in dev, the job sales/nightly.");
    expect(dialog).not.toHaveTextContent("Task tokens minted with it");
    await shell.user.click(within(dialog).getByRole("button", { name: "Revoke the token" }));

    expect(await screen.findByText("No live task token.")).toBeVisible();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "0 task tokens" })).toHaveFocus();
    });
  });

  it("closing a confirmation revokes nothing", async () => {
    const shell = renderShell(PATH, { routes: tokenRoutes() });

    await shell.user.click(await screen.findByRole("button", { name: "Revoke laptop of maria" }));
    await shell.user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );
    await shell.user.click(
      screen.getByRole("button", {
        name: "Revoke the task token of ci-bot in dev, minted by hand",
      }),
    );
    await shell.user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(shell.server.sent.some((request) => request.method === "DELETE")).toBe(false);
  });

  it("names the commands", async () => {
    renderShell(PATH, { routes: tokenRoutes() });

    expect(
      await screen.findByText(
        "lakana task-token mint PRINCIPAL --env ENVIRONMENT --scope 'PATTERN' --ttl SECONDS",
      ),
    ).toBeVisible();
    expect(screen.getByText("lakana token issue NAME LABEL")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Copy command" })).toHaveLength(2);
  });

  it("a person who is not an administrator cannot use Tokens", async () => {
    const shell = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: tokenRoutes(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Tokens" }),
    ).toBeVisible();
    expect(
      shell.server.sent.filter(
        (request) =>
          request.path.startsWith("/api/v1/tokens") ||
          request.path.startsWith("/api/v1/task-tokens"),
      ),
    ).toEqual([]);
  });

  it("reads the personal tokens again on their panel's Refresh, the count its heading", async () => {
    const { server, user } = renderShell(PATH, { routes: tokenRoutes() });
    await screen.findByRole("region", { name: "Live personal tokens" });
    expect(
      screen.getByRole("heading", { level: 2, name: /personal tokens?$/ }),
    ).toBeInTheDocument();
    const reads = (): number =>
      server.sent.filter(
        (request) => request.method === "GET" && request.path.startsWith("/api/v1/tokens"),
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

  it("reads the task tokens again on their panel's Refresh, keeping the environment", async () => {
    const { server, user } = renderShell(`${PATH}?environment=dev`, { routes: tokenRoutes() });
    await screen.findByRole("region", { name: "Live personal tokens" });
    const reads = (): number =>
      server.sent.filter((request) => request.path.startsWith("/api/v1/task-tokens")).length;
    await waitFor(() => {
      expect(reads()).toBeGreaterThan(0);
    });
    const before = reads();
    const buttons = await screen.findAllByRole("button", { name: "Refresh" });

    await user.click(buttons[1] as HTMLElement);

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
    expect(
      server.sent.filter((request) => request.path.startsWith("/api/v1/task-tokens")).at(-1)?.path,
    ).toContain("environment=dev");
  });
});
