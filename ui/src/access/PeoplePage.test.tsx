import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { accessStatusRoute, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeAnswer, FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PEOPLE = [
  { name: "kim", type: "USER", is_active: true },
  { name: "loader", type: "SERVICE", is_active: true },
  { name: "old", type: "USER", is_active: false },
];

describe("PeoplePage", () => {
  it("lists people; every row opens the person", async () => {
    const { user, router } = renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": { status: 200, body: PEOPLE },
        "GET /api/v1/principals/kim/permissions": {
          status: 200,
          body: { permissions: [] },
        },
        "GET /api/v1/principals/kim/passkeys": { status: 200, body: [] },
        "GET /api/v1/principals/kim/sessions": { status: 200, body: [] },
      },
    });

    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(within(row).getByRole("link")).toBeInTheDocument();
      expect(within(row).getByText("›")).toBeInTheDocument();
    }
    expect(within(rows[1] as HTMLElement).getByText("Service account")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Deactivated")).toHaveClass(
      "pill",
      "pill-warn",
    );
    // An active person's state is named too, quietly: no row has a blank state.
    expect(within(rows[0] as HTMLElement).getByText("Active")).toHaveClass("pill", "pill-neutral");
    expect(screen.getByRole("link", { name: "People" })).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("link", { name: "kim" }));
    expect(router.state.location.pathname).toBe("/settings/access/people/kim");
  });

  it("finds people by part of their name", async () => {
    const { user, server } = renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": (request) => ({
          status: 200,
          body: request.path.includes("q=ki") ? PEOPLE.slice(0, 1) : PEOPLE,
        }),
      },
    });
    await screen.findByRole("link", { name: "loader" });

    await user.type(screen.getByRole("searchbox", { name: "Find people" }), "ki");

    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(2);
    });
    expect(server.sent.at(-1)?.path).toBe("/api/v1/principals?limit=200&q=ki");
    // Typing waits for a pause: no request for the half-typed "k".
    expect(server.sent.some((request) => request.path.endsWith("q=k"))).toBe(false);
  });

  it("says when the list shows only the first 200, as the server's next cursor tells", async () => {
    const many = Array.from({ length: 200 }, (_, index) => ({
      name: `user${String(index)}`,
      type: "USER",
      is_active: true,
    }));
    renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": {
          status: 200,
          body: many,
          headers: { "X-Next-Cursor": "next" },
        },
      },
    });

    expect(await screen.findByText(/Showing the first 200/)).toBeInTheDocument();
    expect(screen.getByText("The first 200 people")).toBeInTheDocument();
  });

  it("counts exactly 200 people as all of them when the server has no more", async () => {
    const all = Array.from({ length: 200 }, (_, index) => ({
      name: `user${String(index)}`,
      type: "USER",
      is_active: true,
    }));
    renderShell("/settings/access/people", {
      routes: { "GET /api/v1/principals": { status: 200, body: all } },
    });

    expect(await screen.findByText("200 people")).toBeInTheDocument();
    expect(screen.queryByText(/Showing the first 200/)).toBeNull();
  });

  it("reads the people again on Refresh, showing that it waits", async () => {
    let reads = 0;
    let answer: (value: FakeAnswer) => void = () => undefined;
    const { server, user } = renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": () => {
          reads += 1;
          return reads === 1
            ? { status: 200, body: [{ name: "kim", type: "USER", is_active: true }] }
            : new Promise<FakeAnswer>((resolve) => {
                answer = resolve;
              });
        },
      },
    });
    expect(await screen.findByText("1 person")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(reads).toBe(2);
    });
    expectPending(screen.getByRole("button", { name: /^Refresh/ }));
    answer({ status: 200, body: [{ name: "kim", type: "USER", is_active: true }] });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Refresh" })).not.toHaveAttribute("data-pending");
    });
    expect(server.sent.at(-1)?.path).toBe("/api/v1/principals?limit=200");
  });

  it("says when nobody matches", async () => {
    renderShell("/settings/access/people", {
      routes: { "GET /api/v1/principals": { status: 200, body: [] } },
    });

    expect(await screen.findByText("Nobody matches.")).toBeInTheDocument();
  });

  it("shows each person's picture, or their initials without one, fetching nothing for them", async () => {
    const { server } = renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": {
          status: 200,
          body: [
            { name: "kim.lee", type: "USER", is_active: true, picture: "e1" },
            { name: "loader", type: "SERVICE", is_active: true, picture: null },
          ],
        },
        "GET /api/v1/principals/kim.lee/picture": {
          status: 200,
          blob: new Blob(["webp"], { type: "image/webp" }),
        },
      },
    });

    const rows = (await screen.findAllByRole("row")).slice(1);
    await waitFor(() => {
      expect(rows[0]?.querySelector("img")?.getAttribute("src")).toMatch(/^blob:/);
    });
    expect(rows[1]?.querySelector("img")).toBeNull();
    expect(within(rows[1] as HTMLElement).getByText("L")).toHaveAttribute("aria-hidden", "true");
    // The picture is decorative beside the name: the link is named by the name alone.
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "kim.lee" })).toBeVisible();
    expect(server.sent.filter((request) => request.path.endsWith("/picture"))).toHaveLength(1);
    // The picture stands beside both lines, so the second line starts under the name.
    const cell = rows[1]?.querySelector(".person-cell");
    expect([...(cell?.children ?? [])].map((child) => child.className)).toEqual([
      "avatar avatar-24",
      "row-name",
      "second",
    ]);
  });

  it("marks a hidden character of a person's name", async () => {
    renderShell("/settings/access/people", {
      routes: {
        "GET /api/v1/principals": {
          status: 200,
          body: [{ name: "k\u202Eim", type: "USER", is_active: true }],
        },
      },
    });

    expect(await screen.findByRole("link", { name: "kU+202Eim" })).toBeInTheDocument();
    expect(screen.getByRole("table").textContent).not.toMatch(/[\u202E\u2066]/u);
  });
});

describe("PeoplePage's sort", () => {
  it("sorts by name and by state on the server and keeps the order in the address", async () => {
    const { user, server, router } = renderShell("/settings/access/people", {
      routes: { "GET /api/v1/principals": { status: 200, body: PEOPLE } },
    });
    await screen.findByRole("link", { name: "kim" });
    expect(screen.getByRole("columnheader", { name: /State/u })).not.toHaveAttribute("aria-sort");

    await user.click(screen.getByRole("button", { name: /^State/u }));
    await waitFor(() => {
      expect(server.sent.at(-1)?.path).toBe("/api/v1/principals?limit=200&sort=status&order=asc");
    });
    expect(router.state.location.search).toBe("?sort=status");
    await user.click(screen.getByRole("button", { name: /^State/u }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?sort=status&order=desc");
    });
    expect(await screen.findByRole("columnheader", { name: /State/u })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });
});

describe("PeoplePage's New service account", () => {
  /**
   * The routes of People with two principals, no managed scope, and more that differ.
   *
   * @param more - Routes that differ.
   * @returns The routes.
   */
  function peopleRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
    return {
      ...accessStatusRoute(),
      "GET /api/v1/principals": { status: 200, body: PEOPLE.slice(0, 2) },
      ...more,
    };
  }

  it("creates a service account after the passkey touch, asked first", async () => {
    fakeWebAuthn();
    let created = false;
    const { user, server } = renderShell("/settings/access/people", {
      routes: peopleRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/principals": () => {
          created = true;
          return {
            status: 201,
            body: { name: "ci-loader", type: "SERVICE_ACCOUNT", is_active: true, picture: null },
          };
        },
        "GET /api/v1/principals": () => ({
          status: 200,
          body: created
            ? [{ name: "ci-loader", type: "SERVICE_ACCOUNT", is_active: true }, ...PEOPLE]
            : PEOPLE,
        }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = await screen.findByRole("dialog", { name: "New service account" });
    expect(within(dialog).getByText(/It can do nothing until it has a grant/)).toBeVisible();
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "  ci-loader ");
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));

    expect(await screen.findByRole("link", { name: "ci-loader" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByText(
        "Created the service account ci-loader. It can do nothing until it has a grant.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "People" })).toHaveFocus();
    });
    const writes = server.sent.filter(
      (request) => request.method !== "GET" || request.path === "/api/v1/sign-in/session",
    );
    expect(writes.map((request) => `${request.method} ${request.path}`).slice(-4)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/principals",
    ]);
    expect(writes.at(-1)?.body).toEqual({ name: "ci-loader", type: "SERVICE_ACCOUNT" });
  });

  it("holds Create until a name is typed, and saying why", async () => {
    const { user, server } = renderShell("/settings/access/people", { routes: peopleRoutes() });

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = await screen.findByRole("dialog", { name: "New service account" });
    const create = within(dialog).getByRole("button", { name: "Create service account" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription("Type a name first.");
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "   ");
    expect(create).toBeDisabled();
    // A blank name sends nothing, whatever submits the form.
    within(dialog).getByRole("textbox").closest("form")?.requestSubmit();
    expect(screen.getByRole("dialog", { name: "New service account" })).toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "POST")).toBe(false);
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "x");
    expect(create).toBeEnabled();
  });

  it("keeps the dialog open while the account is being created", async () => {
    const answer = held();
    const { user, server } = renderShell("/settings/access/people", {
      routes: peopleRoutes({ "POST /api/v1/principals": answer.route }),
    });

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = await screen.findByRole("dialog", { name: "New service account" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "ci-loader");
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));
    await user.keyboard("{Escape}");
    // Enter again while it is on its way sends nothing more.
    within(dialog).getByRole("textbox").closest("form")?.requestSubmit();

    expect(screen.getByRole("dialog", { name: "New service account" })).toBeInTheDocument();
    expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
    answer.release({ status: 201, body: { name: "ci-loader", type: "SERVICE_ACCOUNT" } });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("keeps the dialog with the server's sentence on a taken name, which takes the focus", async () => {
    const { user } = renderShell("/settings/access/people", {
      routes: peopleRoutes({
        "POST /api/v1/principals": {
          status: 409,
          body: { detail: "Principal 'kim' already exists.", type: "AlreadyExistsError" },
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = await screen.findByRole("dialog", { name: "New service account" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "kim");
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Principal 'kim' already exists.");
    expect(alert).toHaveFocus();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("kim");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "New service account" })).toHaveFocus();
    });
  });

  it("a managed instance offers no new service account, and names the file", async () => {
    renderShell("/settings/access/people", {
      routes: peopleRoutes(accessStatusRoute(["instance"])),
    });

    expect(
      await screen.findByText(/The whole instance is managed by access.yaml/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'lakana principal create NAME --type service-account --override-managed --reason "WHY"',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New service account" })).toBeNull();
    expect(await screen.findByRole("link", { name: "kim" })).toBeInTheDocument();
  });

  it("offers no new service account until the managed scopes are read", async () => {
    const status = held();
    renderShell("/settings/access/people", {
      routes: peopleRoutes({ "GET /api/v1/access/status": () => status.route() }),
    });

    expect(await screen.findByRole("link", { name: "kim" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New service account" })).toBeNull();
    status.release({ status: 200, body: { scopes: [], overrides: [] } });
    expect(await screen.findByRole("button", { name: "New service account" })).toBeInTheDocument();
  });

  it("says why no account can be created when the managed scopes cannot be read", async () => {
    let tries = 0;
    const { user } = renderShell("/settings/access/people", {
      routes: peopleRoutes({
        "GET /api/v1/access/status": () => {
          tries += 1;
          return tries === 1
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: { scopes: [], overrides: [] } };
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "nothing can be changed here for now: The database is down.",
    );
    expect(screen.queryByRole("button", { name: "New service account" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "New service account" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "People" })).toHaveFocus();
  });
});
