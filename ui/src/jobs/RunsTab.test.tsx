import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, MemoryRouter, RouterProvider, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJob, aJobRun, ENVIRONMENTS } from "../test/jobs";
import { held } from "../test/pending";
import { type FakeRoute, fakeServer } from "../test/server";
import { expectServerSorts } from "../test/sorting";
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
 * Renders the tab in a router that can go back, as the browser's Back does.
 *
 * @returns The router.
 */
function renderRouted() {
  fakeServer({
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    [RUNS]: page([older]),
  });
  const router = createMemoryRouter(
    [
      {
        path: "/jobs/runs",
        element: (
          <>
            <RunsTab />
            <Address />
          </>
        ),
      },
    ],
    { initialEntries: ["/jobs/runs"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

/**
 * Renders the tab at an address over a server.
 *
 * @param runs - What `GET /api/v1/runs` answers.
 * @param path - The address.
 * @param more - Further routes, such as the environments' jobs.
 * @returns The server and a user-event session.
 */
function renderTab(runs: FakeRoute, path = "/jobs/runs", more: Record<string, FakeRoute> = {}) {
  const server = fakeServer({
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    [RUNS]: runs,
    ...more,
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

/**
 * A filter's menu button; a column header that sorts carries the same word.
 *
 * @param name - The filter's name.
 * @returns The button.
 */
function filterMenu(name: RegExp): HTMLElement {
  const button = screen
    .getAllByRole("button", { name })
    .find((candidate) => candidate.closest("th") === null);
  if (button === undefined) {
    throw new Error(`no filter ${String(name)}`);
  }
  return button;
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

  it("each filter is a step back, a job typed key by key one of them, and Back empties the field", async () => {
    const router = renderRouted();
    const user = userEvent.setup();
    await screen.findByRole("region", { name: "Runs listed" });

    await user.click(screen.getByRole("button", { name: /Trigger/u }));
    await user.click(screen.getByRole("option", { name: "Schedule" }));
    const field = screen.getByRole("textbox", { name: "Job" });
    await user.type(field, "daily_sales{Enter}");
    expect(screen.getByTestId("address")).toHaveTextContent(
      /^\?trigger=schedule&job=daily_sales$/u,
    );

    expect(field).toHaveFocus();
    act(() => {
      void router.navigate(-1);
    });
    await waitFor(() => {
      expect(screen.getByTestId("address")).toHaveTextContent(/^\?trigger=schedule$/u);
    });
    expect(field).toHaveValue("");
    act(() => {
      void router.navigate(-1);
    });
    await waitFor(() => {
      expect(screen.getByTestId("address")).toBeEmptyDOMElement();
    });
    expect(screen.getByRole("button", { name: /Trigger/u })).toHaveTextContent("All");
  });

  it("empties the field when Back comes before the name typed was shown", async () => {
    const router = renderRouted();
    const user = userEvent.setup();
    await screen.findByRole("region", { name: "Runs listed" });
    const field = screen.getByRole("textbox", { name: "Job" });
    await user.type(field, "daily_sales");

    // Enter and Back in one batch: the address goes to the name and back before a render.
    act(() => {
      fireEvent.keyDown(field, { key: "Enter" });
      void router.navigate(-1);
    });
    expect(screen.getByTestId("address")).toBeEmptyDOMElement();
    expect(field).toHaveValue("");
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

    await user.click(filterMenu(/State/u));
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

  it("Read again shows no old rows while it reads, then the list with the focus in it", async () => {
    let reads = 0;
    const again = held();
    const { user } = renderTab(async (request) => {
      if (request.path.includes("cursor=c1")) {
        return { status: 200, body: { items: [oldest], next_cursor: null } };
      }
      reads += 1;
      return reads === 1
        ? { status: 200, body: { items: [newer, older], next_cursor: "c1" } }
        : await again.route();
    });
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await user.click(await screen.findByRole("button", { name: "Read again" }));

    const status = await screen.findByText("Reading the runs again from the first page…");
    expect(status).toHaveFocus();
    expect(screen.queryByRole("region", { name: "Runs listed" })).toBeNull();
    expect(screen.queryByRole("link", { name: "orders_hourly" })).toBeNull();

    again.release({
      status: 200,
      body: {
        items: [aJobRun({ id: "r-fresh", job: "weekly_report", tasks: [] })],
        next_cursor: null,
      },
    });
    const region = await screen.findByRole("region", { name: "Runs listed" });
    expect(within(region).getByRole("link", { name: "weekly_report" })).toBeInTheDocument();
    expect(within(region).queryByRole("link", { name: "orders_hourly" })).toBeNull();
    await waitFor(() => {
      expect(region).toHaveFocus();
    });
    expect(screen.queryByText("Reading the runs again from the first page…")).toBeNull();
  });

  it("says a failed Read again, with Try again", async () => {
    let reads = 0;
    const { user } = renderTab((request) => {
      if (request.path.includes("cursor=c1")) {
        return { status: 200, body: { items: [oldest], next_cursor: null } };
      }
      reads += 1;
      return reads === 1
        ? { status: 200, body: { items: [newer], next_cursor: "c1" } }
        : { status: 503, body: { type: "Unavailable", detail: "The server is busy." } };
    });
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await user.click(await screen.findByRole("button", { name: "Read again" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");
    expect(screen.queryByText("Reading the runs again from the first page…")).toBeNull();
    expect(screen.queryByRole("region", { name: "Runs listed" })).toBeNull();
    // The focus goes to Try again, never to the page.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Try again" })).toHaveFocus();
    });
  });

  it("hands the focus to the empty sentence when Read again finds no run", async () => {
    let reads = 0;
    const { user } = renderTab((request) => {
      if (request.path.includes("cursor=c1")) {
        return { status: 200, body: { items: [oldest], next_cursor: null } };
      }
      reads += 1;
      return {
        status: 200,
        body:
          reads === 1 ? { items: [newer], next_cursor: "c1" } : { items: [], next_cursor: null },
      };
    });
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await user.click(await screen.findByRole("button", { name: "Read again" }));

    const empty = await screen.findByText("No runs match these filters.");
    await waitFor(() => {
      expect(empty.closest(".empty")).toHaveFocus();
    });
    expect(document.activeElement).not.toBe(document.body);
  });

  it("shows no old error of a filter chosen again before another filter answered", async () => {
    let allReads = 0;
    const failedRead = held();
    const allAgain = held();
    const { user } = renderTab(async (request) => {
      if (request.path.includes("status=failed")) {
        return await failedRead.route();
      }
      allReads += 1;
      return allReads === 1
        ? { status: 503, body: { type: "Unavailable", detail: "The server is busy." } }
        : await allAgain.route();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");

    // A failed, then B, then back to A before B answers.
    await user.click(filterMenu(/State/u));
    await user.click(screen.getByRole("option", { name: "Failed" }));
    await user.click(filterMenu(/State/u));
    await user.click(screen.getByRole("option", { name: "All" }));
    await waitFor(() => {
      expect(allReads).toBe(2);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    allAgain.release({ status: 200, body: { items: [older], next_cursor: null } });
    expect(await screen.findByRole("link", { name: "nightly_sales" })).toBeInTheDocument();
    failedRead.release({ status: 200, body: { items: [], next_cursor: null } });
  });

  it("shows no error of the previous filter under a new one", async () => {
    const next = held();
    const { user } = renderTab(async (request) =>
      request.path.includes("status=failed")
        ? await next.route()
        : { status: 503, body: { type: "Unavailable", detail: "The server is busy." } },
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");

    await user.click(filterMenu(/State/u));
    await user.click(screen.getByRole("option", { name: "Failed" }));
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    next.release({ status: 200, body: { items: [older], next_cursor: null } });
    expect(await screen.findByRole("link", { name: "nightly_sales" })).toBeInTheDocument();
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

    await user.click(screen.getByRole("button", { name: /^Environment/u, expanded: false }));
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

    // The column headers sort; none of them starts anything.
    const buttons = screen
      .getAllByRole("button")
      .filter((button) => button.closest("th") === null)
      .map((button) => button.textContent);
    expect(buttons.some((text) => /run|start/iu.test(text))).toBe(false);
  });

  it("counts the runs and reads them again on Refresh", async () => {
    const { server, user } = renderTab(twoPages);
    await screen.findByRole("region", { name: "Runs listed" });
    expect(await screen.findByText(/^The first \d+ runs?$/)).toBeInTheDocument();
    const reads = (): number =>
      server.sent.filter(
        (request) => request.method === "GET" && request.path.startsWith("/api/v1/runs"),
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

  describe("starts that wait (#786)", () => {
    const recent = (minutes: number): string =>
      new Date(Date.now() - minutes * 60_000).toISOString();
    const waits = (message: string) => ({ code: "job_waits", message });
    const jobs = (): Record<string, FakeRoute> => ({
      "GET /api/v1/environments/prod/jobs": {
        status: 200,
        body: [
          aJob({
            name: "hourly_stock",
            state: "waiting",
            waiting_for: waits("A start waits for run 1b2c3d4e-0000 to finish."),
            waits_for_run_id: "1b2c3d4e-0000",
            waiting_since: recent(8),
            waiting_trigger: "manual",
          }),
          aJob({ name: "nightly_sales" }),
          aJob({ name: "fresh", last_run_id: null, last_run_status: null, last_run_at: null }),
        ],
      },
      "GET /api/v1/environments/dev/jobs": {
        status: 200,
        body: [
          aJob({
            name: "orders_daily",
            environment: "dev",
            state: "waiting to start",
            waiting_for: { code: "job_starts_soon", message: "It begins within a few seconds." },
            waiting_since: recent(2),
            waiting_trigger: "schedule",
          }),
        ],
      },
    });

    /**
     * The rows of the list, each as its first link's text.
     *
     * @returns The names.
     */
    async function listed(): Promise<(string | undefined)[]> {
      const region = await screen.findByRole("region", { name: "Runs listed" });
      return within(region)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("link")[0]?.textContent);
    }

    it("lists every waiting start above the runs as Waiting, newest request first", async () => {
      renderTab(page([older]), "/jobs/runs", jobs());

      await waitFor(async () => {
        expect(await listed()).toEqual(["orders_daily", "hourly_stock", "nightly_sales"]);
      });
      const region = screen.getByRole("region", { name: "Runs listed" });
      const [soon, busy] = within(region).getAllByRole("row").slice(1);
      expect(within(busy as HTMLElement).getByText("Waiting")).toBeInTheDocument();
      expect(busy).toHaveTextContent("Environment prod");
      expect(busy).toHaveTextContent("By hand");
      expect(busy).toHaveTextContent("Not started yet");
      expect(
        within(busy as HTMLElement).getByRole("link", { name: "hourly_stock" }),
      ).toHaveAttribute("href", "/jobs/prod/job/analytics/hourly_stock");
      expect(
        within(busy as HTMLElement).getByRole("link", { name: "waits for run 1b2c3d4e" }),
      ).toHaveAttribute("href", "/jobs/prod/runs/1b2c3d4e-0000");
      expect(busy?.querySelector("[data-tip]")).toHaveAttribute(
        "data-tip",
        "A start waits for run 1b2c3d4e-0000 to finish.",
      );
      expect(soon).toHaveTextContent("Schedule");
      expect(soon).toHaveTextContent("starts within seconds");
      expect(within(soon as HTMLElement).getAllByRole("link")).toHaveLength(1);
      // The count holds the waiting starts, which become runs.
      expect(screen.getByText(/^3 runs$/)).toBeInTheDocument();
    });

    it("leaves them out under a state, trigger, job or period they do not match", async () => {
      const cases: [string, string[]][] = [
        ["status=succeeded", ["nightly_sales"]],
        ["status=queued%2Crunning", ["orders_daily", "hourly_stock", "nightly_sales"]],
        ["trigger=schedule", ["orders_daily", "nightly_sales"]],
        ["job=hourly_stock", ["hourly_stock", "nightly_sales"]],
        ["environment=prod", ["hourly_stock", "nightly_sales"]],
      ];
      for (const [query, names] of cases) {
        cleanup();
        renderTab(page([older]), `/jobs/runs?${query}`, jobs());
        await waitFor(async () => {
          expect(await listed()).toEqual(names);
        });
      }
    });

    it("shows a waiting start when no run matches, and runs alone when jobs cannot be read", async () => {
      renderTab(page([]), "/jobs/runs", jobs());
      await waitFor(async () => {
        expect(await listed()).toEqual(["orders_daily", "hourly_stock"]);
      });
      expect(screen.queryByText("No runs match these filters.")).toBeNull();

      cleanup();
      renderTab(page([older]), "/jobs/runs");
      await waitFor(async () => {
        expect(await listed()).toEqual(["nightly_sales"]);
      });
      await settle();
      expect(screen.queryByRole("alert")).toBeNull();
    });

    /**
     * The reads of an environment's jobs, in order.
     *
     * @param sent - The requests.
     * @returns How many there were.
     */
    const jobReads = (sent: readonly { method: string; path: string }[]): number =>
      sent.filter((request) => /^\/api\/v1\/environments\/[^/]+\/jobs$/u.test(request.path)).length;

    it("reads no jobs when the State shows no waiting start", async () => {
      const { server } = renderTab(page([older]), "/jobs/runs?status=succeeded", jobs());
      await waitFor(async () => {
        expect(await listed()).toEqual(["nightly_sales"]);
      });
      await settle();
      expect(jobReads(server.sent)).toBe(0);
    });

    it("reads the waiting starts with the first page: on Refresh, not again on Show more", async () => {
      let started = false;
      const answers = jobs();
      const { server, user } = renderTab(
        (request) =>
          request.path.includes("cursor=c1")
            ? { status: 200, body: { items: [oldest], next_cursor: null } }
            : {
                status: 200,
                body: {
                  items: started
                    ? [
                        aJobRun({
                          id: "r-stock",
                          job: "hourly_stock",
                          status: "queued",
                          tasks: [],
                        }),
                        older,
                      ]
                    : [older],
                  next_cursor: "c1",
                },
              },
        "/jobs/runs",
        {
          ...answers,
          "GET /api/v1/environments/prod/jobs": () =>
            started
              ? { status: 200, body: [aJob({ name: "hourly_stock" })] }
              : (answers["GET /api/v1/environments/prod/jobs"] as {
                  status: number;
                  body: unknown;
                }),
        },
      );
      await waitFor(async () => {
        expect(await listed()).toEqual(["orders_daily", "hourly_stock", "nightly_sales"]);
      });
      const before = jobReads(server.sent);

      // The start has begun: Refresh reads its run and the starts together, so it shows once.
      started = true;
      await user.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);
      await waitFor(async () => {
        expect(await listed()).toEqual(["orders_daily", "hourly_stock", "nightly_sales"]);
        expect(screen.getAllByText("Waiting")).toHaveLength(1);
      });
      expect(jobReads(server.sent)).toBeGreaterThan(before);

      // Show more goes on from that first page and its starts; it reads no jobs of its own.
      const shown = jobReads(server.sent);
      await user.click(screen.getByRole("button", { name: "Show more" }));
      await waitFor(async () => {
        expect(await listed()).toEqual([
          "orders_daily",
          "hourly_stock",
          "nightly_sales",
          "nightly_sales",
        ]);
      });
      expect(jobReads(server.sent)).toBe(shown);
    });

    it("holds the starts against the Period, newest request first, one of unknown time last", async () => {
      const old = new Date(Date.now() - 2 * 86_400_000).toISOString();
      const starts: Record<string, FakeRoute> = {
        "GET /api/v1/environments/prod/jobs": {
          status: 200,
          body: [
            aJob({
              name: "unknown_time",
              state: "waiting to start",
              waiting_for: waits("It begins within a few seconds."),
              waiting_since: null,
              waiting_trigger: null,
            }),
            aJob({
              name: "two_days_ago",
              state: "waiting",
              waiting_for: waits("A start waits."),
              waits_for_run_id: "1b2c3d4e-0000",
              waiting_since: old,
              waiting_trigger: "schedule",
            }),
            aJob({
              name: "just_now",
              state: "waiting",
              waiting_for: waits("A start waits."),
              waits_for_run_id: "1b2c3d4e-0000",
              waiting_since: recent(1),
              waiting_trigger: "api",
            }),
          ],
        },
      };
      renderTab(page([]), "/jobs/runs?environment=prod&period=24h", starts);
      await waitFor(async () => {
        expect(await listed()).toEqual(["just_now", "unknown_time"]);
      });

      cleanup();
      renderTab(page([]), "/jobs/runs?environment=prod&period=all", starts);
      await waitFor(async () => {
        expect(await listed()).toEqual(["just_now", "two_days_ago", "unknown_time"]);
      });
    });

    it("reads the waiting starts again with Read again", async () => {
      const { server, user } = renderTab(twoPages, "/jobs/runs", jobs());
      await user.click(await screen.findByRole("button", { name: "Show more" }));
      const again = await screen.findByRole("button", { name: "Read again" });
      const before = jobReads(server.sent);

      await user.click(again);
      await waitFor(() => {
        expect(jobReads(server.sent)).toBeGreaterThan(before);
      });
      await waitFor(async () => {
        expect((await listed()).slice(0, 2)).toEqual(["orders_daily", "hourly_stock"]);
      });
    });

    it("says Unknown for a start whose trigger the server could not read", async () => {
      renderTab(page([]), "/jobs/runs?environment=prod", {
        "GET /api/v1/environments/prod/jobs": {
          status: 200,
          body: [
            aJob({
              name: "hourly_stock",
              state: "waiting to start",
              waiting_for: waits("It begins within a few seconds."),
              waiting_since: null,
              waiting_trigger: null,
            }),
          ],
        },
      });
      await waitFor(async () => {
        expect(await listed()).toEqual(["hourly_stock"]);
      });
      const row = screen.getByRole("link", { name: "hourly_stock" }).closest("tr") as HTMLElement;
      expect(row).not.toHaveTextContent("By hand");
      expect(within(row).getAllByText("Unknown").length).toBeGreaterThan(0);
    });
  });
});

describe("RunsTab's sort", () => {
  it("sorts by a column on the server, keeps the filters and the order in the address", async () => {
    const { server, user } = renderTab(page([older]), "/jobs/runs?status=failed");
    await screen.findByRole("region", { name: "Runs listed" });
    const created = screen.getByRole("columnheader", { name: /Created/u });
    // The list's own order is the newest first: the Created column says so.
    expect(created).toHaveAttribute("aria-sort", "descending");

    await user.click(within(created).getByRole("button"));
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("order")).toBe("asc");
    });
    const read = runReads(server.sent).at(-1);
    expect(read?.get("sort")).toBe("created");
    expect(read?.get("status")).toBe("failed");
    expect(read?.has("cursor")).toBe(false);
    expect(screen.getByTestId("address")).toHaveTextContent(
      "?status=failed&sort=created&order=asc",
    );
    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /Created/u })).toHaveAttribute(
        "aria-sort",
        "ascending",
      );
    });
    // The list was drawn again: the header pressed keeps the focus.
    expect(screen.getByRole("button", { name: /Created/u })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: /^State/u }));
    await waitFor(() => {
      expect(runReads(server.sent).at(-1)?.get("sort")).toBe("status");
    });
    expect(screen.getByTestId("address")).toHaveTextContent("?status=failed&sort=status");
  });

  it("sorts by the environment, what started it, duration and tasks", async () => {
    const { server, user } = renderTab(page([older]));
    await screen.findByRole("region", { name: "Runs listed" });
    // The region is drawn again with each new order.
    const region = (): HTMLElement => screen.getByRole("region", { name: "Runs listed" });
    await expectServerSorts(user, region, server.sent, "/api/v1/runs", [
      ["Started by", "sort=trigger&order=asc"],
      ["Environment", "sort=environment&order=asc"],
      ["Duration", "sort=duration&order=desc"],
      ["Tasks", "sort=tasks&order=desc"],
    ]);
  });

  it("opens a shared link sorted the same way", async () => {
    const { server } = renderTab(page([older]), "/jobs/runs?sort=name&order=desc");
    await screen.findByRole("region", { name: "Runs listed" });
    const read = runReads(server.sent)[0];
    expect([read?.get("sort"), read?.get("order")]).toEqual(["name", "desc"]);
    expect(screen.getByRole("columnheader", { name: /Run/u })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });
});

describe("RunsTab's pages in an order by a value that changes", () => {
  it("shows once a run that a later page repeats", async () => {
    const { user } = renderTab((request) =>
      request.path.includes("cursor=c1")
        ? { status: 200, body: { items: [older, oldest], next_cursor: null } }
        : { status: 200, body: { items: [newer, older], next_cursor: "c1" } },
    );
    const region = await screen.findByRole("region", { name: "Runs listed" });
    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    });
    expect(within(region).getAllByRole("row")).toHaveLength(4);
  });
});
