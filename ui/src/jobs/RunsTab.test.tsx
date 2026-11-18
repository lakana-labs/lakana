import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJobRun, ENVIRONMENTS } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { RunsTab } from "./RunsTab";

const RUNS = "GET /api/v1/runs";

/**
 * Shows the address the tab is at.
 *
 * @returns The address's query.
 */
function Address() {
  const location = useLocation();
  return <output data-testid="address">{location.search}</output>;
}

/**
 * Renders the tab at an address over a server.
 *
 * @param runs - What `GET /api/v1/runs` answers.
 * @param path - The address.
 * @returns The server and a user-event session.
 */
function renderTab(runs: FakeRoute, path = "/jobs/runs") {
  const server = fakeServer({
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    [RUNS]: runs,
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <RunsTab />
      <Address />
    </MemoryRouter>,
  );
  return { server, user: userEvent.setup() };
}

/**
 * The queries of the runs' reads, in order.
 *
 * @param sent - The requests.
 * @returns The query of each read of `/api/v1/runs`.
 */
function runReads(sent: readonly { method: string; path: string }[]): URLSearchParams[] {
  return sent
    .filter((request) => request.path.startsWith("/api/v1/runs"))
    .map((request) => new URLSearchParams(request.path.split("?")[1]));
}

const page = (items: readonly RunOut[], next: string | null = null): FakeRoute => ({
  status: 200,
  body: { items, next_cursor: next },
});

/**
 * Lets the reads and their renders finish.
 */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 6; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
    });
  }
}

const newer = aJobRun({ id: "r-new", environment: "dev", job: "orders_hourly", tasks: [] });
const older = aJobRun({ id: "r-old", created_at: "2026-11-16T01:00:00Z", tasks: [] });
const oldest = aJobRun({ id: "r-oldest", created_at: "2026-11-10T01:00:00Z", tasks: [] });

/**
 * Two pages: the first with the newer runs, the second behind the cursor `c1`.
 *
 * @param request - The read.
 * @returns The page it asks for.
 */
const twoPages: FakeRoute = (request) =>
  request.path.includes("cursor=c1")
    ? { status: 200, body: { items: [oldest], next_cursor: null } }
    : { status: 200, body: { items: [newer, older], next_cursor: "c1" } };

afterEach(() => {
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("RunsTab", () => {
  it("lists runs of every environment, newest first, with Show more", async () => {
    const { server, user } = renderTab(twoPages);

    const region = await screen.findByRole("region", { name: "Runs listed" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("link")[0]?.textContent)).toEqual([
      "orders_hourly",
      "nightly_sales",
    ]);
    expect(rows[0]).toHaveTextContent("Environment dev");
    expect(rows[1]).toHaveTextContent("Environment prod");

    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(within(region).getAllByRole("row")).toHaveLength(4);
    });
    // The focus goes to the first new row, never to the page.
    await waitFor(() => {
      expect(within(region).getAllByRole("link").at(-1)).toHaveFocus();
    });
    const reads = runReads(server.sent);
    expect(reads[0]?.has("environment")).toBe(false);
    expect(reads[0]?.get("limit")).toBe("50");
    expect(reads[0]?.get("since")).not.toBeNull();
    expect(reads.at(-1)?.get("cursor")).toBe("c1");
    expect(server.sent.at(-1)?.headers["x-lakana-background"]).toBeUndefined();
  });

  it("sends the filters the address holds", async () => {
    const { server, user } = renderTab(
      page([older]),
      "/jobs/runs?environment=prod&status=queued%2Crunning&trigger=manual&job=nightly_sales&period=all",
    );

    await screen.findByRole("region", { name: "Runs listed" });
    const first = runReads(server.sent)[0];
    expect(first?.get("environment")).toBe("prod");
    expect(first?.get("status")).toBe("queued,running");
    expect(first?.get("trigger")).toBe("manual");
    expect(first?.get("job")).toBe("nightly_sales");
    expect(first?.has("since")).toBe(false);

    await user.click(screen.getByRole("button", { name: /Trigger/u }));
    await user.click(screen.getByRole("option", { name: "After a deploy" }));
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("trigger")).toBe("deploy");
    });
    expect(screen.getByTestId("address")).toHaveTextContent("trigger=deploy");
  });

  it("applies a job name on Enter and when the field is left", async () => {
    const { server, user } = renderTab(page([older]));
    await screen.findByRole("region", { name: "Runs listed" });

    const field = screen.getByRole("textbox", { name: "Job" });
    await user.type(field, "daily_sales");
    expect(runReads(server.sent).at(-1)?.has("job")).toBe(false);
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("job")).toBe("daily_sales");
    });
    const reads = runReads(server.sent).length;

    // Leaving the field with the name unchanged reads nothing again.
    await user.tab();
    expect(runReads(server.sent)).toHaveLength(reads);

    await user.clear(field);
    await user.type(field, "orders_hourly");
    await user.tab();
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("job")).toBe("orders_hourly");
    });
  });

  it("drops the previous filter's rows at once and says a failed first read at once", async () => {
    let fail = false;
    let open: (() => void) | null = null;
    const { user } = renderTab(async (request) => {
      if (request.path.includes("status=failed")) {
        if (fail) {
          return { status: 503, body: { type: "Unavailable", detail: "The server is busy." } };
        }
        await new Promise<void>((resolve) => {
          open = resolve;
        });
      }
      return { status: 200, body: { items: [older], next_cursor: null } };
    });
    await screen.findByRole("link", { name: "nightly_sales" });

    await user.click(screen.getByRole("button", { name: /State/u }));
    await user.click(screen.getByRole("option", { name: "Failed" }));
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "nightly_sales" })).toBeNull();
    });
    (open as (() => void) | null)?.();

    fail = true;
    await user.click(screen.getByRole("button", { name: /Period/u }));
    await user.click(screen.getByRole("option", { name: "All time" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("stops reading the first page again once more were shown", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server } = renderTab(twoPages);
    await settle();
    expect(runReads(server.sent)).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await settle();
    expect(runReads(server.sent)).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Read again" })).toBeNull();

    act(() => {
      screen.getByRole("button", { name: "Show more" }).click();
    });
    await settle();
    expect(runReads(server.sent)).toHaveLength(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    await settle();
    expect(runReads(server.sent)).toHaveLength(3);
    expect(
      screen.getByText("This list is no longer read again while more runs are shown."),
    ).toBeInTheDocument();
  });

  it("Read again starts over", async () => {
    const { server, user } = renderTab(twoPages);
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    const again = await screen.findByRole("button", { name: "Read again" });
    const before = runReads(server.sent).length;

    await user.click(again);
    await waitFor(() => {
      expect(runReads(server.sent)).toHaveLength(before + 1);
    });
    expect(runReads(server.sent).at(-1)?.has("cursor")).toBe(false);
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(3);
    });
    expect(screen.queryByRole("button", { name: "Read again" })).toBeNull();
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
  });

  it("says no runs match and clears the filters", async () => {
    const { user } = renderTab(page([]), "/jobs/runs?environment=dev&status=failed&job=x");

    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    expect(screen.getByTestId("address")).toHaveTextContent(/^\?environment=dev$/u);
    expect(screen.getByRole("textbox", { name: "Job" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Job" })).toHaveValue("");
    // With nothing left to clear, the sentence stands alone.
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("offers Show more, not Clear filters, for an empty page with more behind it", async () => {
    renderTab(page([], "c1"), "/jobs/runs?status=failed");

    expect(await screen.findByRole("button", { name: "Show more" })).toBeInTheDocument();
    expect(screen.queryByText("No runs match these filters.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("chooses an environment through the shared filter", async () => {
    const { server, user } = renderTab(page([older]));
    await screen.findByRole("region", { name: "Runs listed" });

    await user.click(screen.getByRole("button", { name: /^Environment/u }));
    await user.click(screen.getByRole("option", { name: /^prod/u }));
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("environment")).toBe("prod");
    });
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("prod");
  });

  it("offers no control to start a run", async () => {
    renderTab(
      page([aJobRun({ kind: "selection", selection: { patterns: ["silver.*"] }, job: null })]),
    );
    await screen.findByRole("region", { name: "Runs listed" });

    const buttons = screen.getAllByRole("button").map((button) => button.textContent);
    expect(buttons.some((text) => /run|start/iu.test(text))).toBe(false);
  });
});
