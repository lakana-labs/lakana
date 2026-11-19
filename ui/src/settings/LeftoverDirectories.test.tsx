import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OrphanedLocationOut, OrphanedLocationsOut } from "../api/environments";
import { initI18n } from "../i18n/i18n";
import { fakeServer, type FakeRoute } from "../test/server";
import { LeftoverDirectories } from "./LeftoverDirectories";

initI18n();

/**
 * A leftover directory.
 *
 * @param over - What differs from an old one of two files.
 * @returns The directory.
 */
const aLocation = (over: Partial<OrphanedLocationOut> = {}): OrphanedLocationOut => ({
  location: "managed/gold/sales-1a2b",
  files: 2,
  bytes: 2_000_000,
  newest_file_at: "2026-09-01T09:00:00Z",
  recent: false,
  deleted: false,
  files_deleted: 0,
  bytes_deleted: 0,
  kept_because: null,
  ...over,
});

/**
 * A search's or a deletion's answer.
 *
 * @param locations - The directories.
 * @param complete - Whether the time limit left nothing to do.
 * @returns The answer.
 */
const report = (
  locations: readonly OrphanedLocationOut[],
  complete = true,
): OrphanedLocationsOut => ({ environment: "dev", older_than_days: 30, locations, complete });

const THREE = [
  aLocation(),
  aLocation({ location: "managed/gold/orders-3c4d", files: 1, bytes: 500_000 }),
  aLocation({
    location: "managed/silver/fresh-5e6f",
    recent: true,
    newest_file_at: null,
    bytes: 1_000,
  }),
];

/**
 * Renders dev's Leftover directories.
 *
 * @param routes - The server's routes.
 * @returns The fake server and a user.
 */
function renderPanel(routes: Record<string, FakeRoute>): {
  readonly server: ReturnType<typeof fakeServer>;
  readonly user: ReturnType<typeof userEvent.setup>;
} {
  const server = fakeServer(routes);
  render(<LeftoverDirectories name="dev" />);
  return { server, user: userEvent.setup() };
}

const FIND = "GET /api/v1/environments/dev/orphaned-locations";
const DELETE = "POST /api/v1/environments/dev/orphaned-locations/delete";

afterEach(() => {
  vi.useRealTimers();
});

describe("LeftoverDirectories", () => {
  it("finds the leftover directories older than the days given", async () => {
    const { server, user } = renderPanel({ [FIND]: { status: 200, body: report(THREE) } });
    expect(screen.getByText(/Older releases could leave table directories/)).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Older than" })).toHaveValue("30");

    await user.click(screen.getByRole("button", { name: "Find" }));

    const region = await screen.findByRole("region", { name: "Leftover directories" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[2]).toHaveTextContent("recent, kept");
    expect(rows[2]).toHaveTextContent("unknown");
    expect(rows[0]).toHaveTextContent("older");
    expect(screen.getByText("3 directories, 2.5 MB")).toBeInTheDocument();
    expect(server.sent[0]?.path).toBe(
      "/api/v1/environments/dev/orphaned-locations?older_than_days=30",
    );
    expect(screen.getByRole("button", { name: "Find again" })).toHaveFocus();
  });

  it("says when nothing is left over", async () => {
    const { user } = renderPanel({ [FIND]: { status: 200, body: report([]) } });

    await user.click(screen.getByRole("button", { name: "Find" }));

    expect(await screen.findByText("Nothing left over.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete the old ones" })).not.toBeInTheDocument();
  });

  it("a partial search continues", async () => {
    const { server, user } = renderPanel({ [FIND]: { status: 200, body: report([], false) } });
    const days = screen.getByRole("textbox", { name: "Older than" });
    await user.clear(days);
    await user.type(days, "60");
    await user.tab();

    await user.click(screen.getByRole("button", { name: "Find" }));
    expect(
      await screen.findByText("The search stopped at the time limit; Find again continues."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find again" }));

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/dev/orphaned-locations?older_than_days=60",
      "/api/v1/environments/dev/orphaned-locations?older_than_days=60",
    ]);
  });

  it("deletes the old ones after a confirmation, without a touch", async () => {
    let calls = 0;
    const { server, user } = renderPanel({
      [FIND]: { status: 200, body: report(THREE) },
      [DELETE]: () => {
        calls += 1;
        return {
          status: 200,
          body: report(
            [
              aLocation({ deleted: true, files_deleted: 2, bytes_deleted: 2_000_000 }),
              aLocation({
                location: "managed/gold/orders-3c4d",
                kept_because: "A file changed since the search.",
              }),
            ],
            calls > 1,
          ),
        };
      },
    });
    await user.click(screen.getByRole("button", { name: "Find" }));
    await user.click(await screen.findByRole("button", { name: "Delete the old ones" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete 2 directories, 2.5 MB?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete them" }));

    const region = await screen.findByRole("region", { name: "Leftover directories" });
    expect(await within(region).findByText("deleted")).toBeInTheDocument();
    expect(within(region).getByText("A file changed since the search.")).toBeInTheDocument();
    expect(screen.getByText("2 directories, 4 MB")).toHaveFocus();
    expect(
      screen.getByText("The deletion stopped at the time limit; Continue deletes the rest."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("button", { name: "Find again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue" })).not.toBeInTheDocument();
    const deletes = server.sent.filter((request) => request.method === "POST");
    expect(deletes.map((request) => request.body)).toEqual([
      { older_than_days: 30 },
      { older_than_days: 30 },
    ]);
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("deletes with the days the list was found with, not the field's new value", async () => {
    const { server, user } = renderPanel({
      [FIND]: { status: 200, body: report(THREE) },
      [DELETE]: { status: 200, body: report([]) },
    });
    await user.click(screen.getByRole("button", { name: "Find" }));
    await screen.findByRole("region", { name: "Leftover directories" });
    const days = screen.getByRole("textbox", { name: "Older than" });
    await user.clear(days);
    await user.type(days, "1");
    await user.tab();
    expect(
      screen.getByText("This list was found with 30 days; Find again to use 1."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete the old ones" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("older than 30 days");
    await user.click(within(dialog).getByRole("button", { name: "Delete them" }));

    expect(await screen.findByText("Nothing left over.")).toBeInTheDocument();
    const post = server.sent.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ older_than_days: 30 });
  });

  it("after a partial search the question says more may go", async () => {
    const { user } = renderPanel({ [FIND]: { status: 200, body: report(THREE, false) } });
    await user.click(screen.getByRole("button", { name: "Find" }));
    await user.click(await screen.findByRole("button", { name: "Delete the old ones" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Delete at least 2 directories, 2.5 MB?",
    });
    expect(dialog).toHaveTextContent(
      "The search stopped at its time limit, so the deletion may remove more than was counted.",
    );
  });

  it("names what a deletion kept without a reason, and Cancel deletes nothing", async () => {
    const { server, user } = renderPanel({
      [FIND]: { status: 200, body: report(THREE) },
      [DELETE]: {
        status: 200,
        body: report([
          aLocation({ location: "managed/silver/fresh-5e6f", recent: true }),
          aLocation({ location: "managed/gold/orders-3c4d" }),
        ]),
      },
    });
    await user.click(screen.getByRole("button", { name: "Find" }));
    const button = await screen.findByRole("button", { name: "Delete the old ones" });
    await user.click(button);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "POST")).toBe(false);

    await user.click(button);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete them" }),
    );

    const region = await screen.findByRole("region", { name: "Leftover directories" });
    expect(await within(region).findByText("recent, kept")).toBeInTheDocument();
    expect(within(region).getByText("kept")).toBeInTheDocument();
  });

  it("a storage that does not answer can be tried again", async () => {
    let answer: FakeRoute = {
      status: 503,
      body: { detail: "The storage of dev did not answer. Ask again in a minute." },
      headers: { "Retry-After": "60" },
    };
    const { user } = renderPanel({ [FIND]: () => answer as never });

    await user.click(screen.getByRole("button", { name: "Find" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("did not answer");
    expect(screen.getByRole("button", { name: "Find" })).toHaveFocus();
    answer = { status: 200, body: report([]) };
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nothing left over.")).toBeInTheDocument();

    answer = {
      status: 502,
      body: { detail: "The storage of dev refused its credentials. Fix them under Storage." },
    };
    await user.click(screen.getByRole("button", { name: "Find again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("refused its credentials");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("says what it waits for after a second", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const pending: (() => void)[] = [];
    /**
     * An answer that comes when the test lets it.
     *
     * @param body - The answer.
     * @returns The route.
     */
    const held =
      (body: OrphanedLocationsOut): FakeRoute =>
      () =>
        new Promise((resolve) => {
          pending.push(() => {
            resolve({ status: 200, body });
          });
        });
    renderPanel({ [FIND]: held(report(THREE)), [DELETE]: held(report([])) });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    /**
     * Lets a second pass, then the held answer come.
     *
     * @param words - What the panel says after the second.
     */
    const waitASecond = async (words: string): Promise<void> => {
      expect(screen.queryByText(words)).not.toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(screen.getByText(words)).toBeInTheDocument();
      await act(async () => {
        pending.shift()?.();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.queryByText(words)).not.toBeInTheDocument();
    };

    await user.click(screen.getByRole("button", { name: "Find" }));
    await waitASecond("Looking through the storage…");
    await user.click(await screen.findByRole("button", { name: "Delete the old ones" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete them" }),
    );
    await waitASecond("Deleting the old directories…");
    expect(await screen.findByText("Nothing left over.")).toBeInTheDocument();
  });

  it("keeps locations whole and marked", async () => {
    const { user } = renderPanel({
      [FIND]: {
        status: 200,
        body: report([aLocation({ location: "managed/gold/sa‮les" })]),
      },
    });

    await user.click(screen.getByRole("button", { name: "Find" }));

    const region = await screen.findByRole("region", { name: "Leftover directories" });
    expect(region).toHaveClass("leftover-scroll");
    expect(region.querySelector(".leftover-location")).toHaveTextContent(
      "managed/gold/saU+202Eles",
    );
  });
});
