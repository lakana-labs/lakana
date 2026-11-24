import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectPending } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeAnswer } from "../test/server";

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
