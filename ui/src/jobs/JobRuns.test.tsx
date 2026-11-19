import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJobRun } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { JobRuns } from "./JobRuns";

const RUNS = "GET /api/v1/environments/prod/runs";
const NOW = Date.parse("2026-11-17T10:00:00Z");
const DAY = 86_400_000;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * Shows where the router is, so a test can read it.
 *
 * @returns The location as text.
 */
function Where() {
  const location = useLocation();
  return <output aria-label="where">{location.search}</output>;
}

/**
 * Renders the runs of `nightly_sales` at an address.
 *
 * @param route - What the server answers for the runs.
 * @param search - The address's query.
 * @returns The fake server, the rows handler and a way to render again with another revision and
 *   current run.
 */
function renderRuns(route: FakeRoute, search = "") {
  const server = fakeServer({ [RUNS]: route });
  const onRows = vi.fn();
  const page = (revision: number, current: RunOut | null) => (
    <MemoryRouter initialEntries={[`/jobs/prod/job/analytics/nightly_sales${search}`]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <JobRuns
                environment="prod"
                repository="analytics"
                job="nightly_sales"
                selected="r2"
                revision={revision}
                current={current}
                onRows={onRows}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>
  );
  const view = render(page(0, null));
  const change = (revision: number, current: RunOut | null): void => {
    view.rerender(page(revision, current));
  };
  return { server, onRows, change };
}

/**
 * The query of each request for runs.
 *
 * @param sent - The requests.
 * @returns Their parameters.
 */
function queries(sent: readonly { readonly path: string }[]): URLSearchParams[] {
  return sent.map((request) => new URL(request.path, "http://localhost").searchParams);
}

/**
 * Lets the answers in flight arrive.
 */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
    });
  }
}

afterEach(() => {
  vi.useRealTimers();
});

describe("JobRuns", () => {
  it("lists runs with glyph, time link, duration, trigger and failed of total", async () => {
    const failed = aJobRun({
      id: "r2",
      status: "failed",
      trigger: "manual",
      failed_tasks: 2,
      total_tasks: 5,
      tasks: [],
    });
    const done = aJobRun({ id: "r1", created_at: "2026-11-16T01:00:00Z", tasks: [] });
    const { onRows } = renderRuns({ status: 200, body: [failed, done] });

    const link = await screen.findByRole("link", { name: when(failed.created_at) });
    expect(link).toHaveAttribute("href", "/jobs/prod/job/analytics/nightly_sales?run=r2");
    expect(link).toHaveAttribute("aria-current", "true");
    const cells = within(link.closest("tr") as HTMLElement).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toEqual([
      "Failed",
      when(failed.created_at),
      "38 s",
      "By hand",
      "2 of 5 tasks failed",
      "›",
    ]);
    const other = screen.getByRole("link", { name: when(done.created_at) });
    expect(other).not.toHaveAttribute("aria-current");
    expect(other.closest("tr")).toHaveTextContent("Schedule");
    expect(other.closest("tr")).not.toHaveTextContent("tasks failed");
    expect(screen.getByRole("region", { name: "Runs of nightly_sales" })).toBeInTheDocument();
    expect(onRows).toHaveBeenLastCalledWith([failed, done]);
  });

  it("writes each trigger in words, and the duration only of a run that ended", async () => {
    renderRuns({
      status: 200,
      body: [
        aJobRun({ id: "a", trigger: "api", created_at: "2026-11-17T04:00:00Z" }),
        aJobRun({ id: "b", trigger: "deploy", created_at: "2026-11-17T03:00:00Z" }),
        aJobRun({
          id: "c",
          trigger: null,
          status: "running",
          finished_at: null,
          created_at: "2026-11-17T02:00:00Z",
        }),
      ],
    });

    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(rows.map((tr) => within(tr).getAllByRole("cell")[3]?.textContent)).toEqual([
      "API",
      "After a deploy",
      "Ad hoc",
    ]);
    const running = within(rows[2] as HTMLElement).getAllByRole("cell")[2] as HTMLElement;
    expect(within(running).getByText("–")).toHaveAttribute("aria-hidden", "true");
    expect(within(running).getByText("Not finished yet")).toHaveClass("visually-hidden");
  });

  it("says when no run is in the period", async () => {
    renderRuns({ status: 200, body: [] });

    expect(await screen.findByText("No run of this job in this period.")).toBeInTheDocument();
  });

  it("filters by status and period through the address, last 30 days by default", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    const { server } = renderRuns({ status: 200, body: [aJobRun({ id: "r2", tasks: [] })] });
    const user = userEvent.setup();

    await screen.findByRole("link", { name: when(aJobRun().created_at) });
    let [query] = queries(server.sent);
    expect(query?.get("job")).toBe("nightly_sales");
    expect(query?.get("repository")).toBe("analytics");
    expect(query?.get("limit")).toBe("50");
    expect(query?.has("status")).toBe(false);
    expect(query?.get("since")).toBe(new Date(NOW - 30 * DAY).toISOString());

    await user.click(screen.getByRole("button", { name: /Status/ }));
    await user.click(screen.getByRole("option", { name: "Failed" }));
    await waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });
    expect(screen.getByLabelText("where")).toHaveTextContent("?status=failed");
    query = queries(server.sent)[1];
    expect(query?.get("status")).toBe("failed");

    await user.click(screen.getByRole("button", { name: /Period/ }));
    await user.click(screen.getByRole("option", { name: "All time" }));
    await waitFor(() => {
      expect(server.sent).toHaveLength(3);
    });
    expect(screen.getByLabelText("where")).toHaveTextContent("?status=failed&period=all");
    query = queries(server.sent)[2];
    expect(query?.has("since")).toBe(false);

    await user.click(screen.getByRole("button", { name: /Status/ }));
    await user.click(screen.getByRole("option", { name: "All" }));
    await waitFor(() => {
      expect(server.sent).toHaveLength(4);
    });
    expect(screen.getByLabelText("where")).toHaveTextContent("?period=all");
  });

  it("reads the filters from the address", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    const { server } = renderRuns({ status: 200, body: [] }, "?status=succeeded&period=24h");

    await screen.findByText("No run of this job in this period.");
    const [query] = queries(server.sent);
    expect(query?.get("status")).toBe("succeeded");
    expect(query?.get("since")).toBe(new Date(NOW - DAY).toISOString());
    expect(screen.getByRole("button", { name: /Status/ })).toHaveTextContent("Succeeded");
    expect(screen.getByRole("button", { name: /Period/ })).toHaveTextContent("Last 24 hours");
  });

  it("takes an address it does not know as the defaults", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    const { server } = renderRuns({ status: 200, body: [] }, "?status=lost&period=year");

    await screen.findByText("No run of this job in this period.");
    const [query] = queries(server.sent);
    expect(query?.has("status")).toBe(false);
    expect(query?.get("since")).toBe(new Date(NOW - 30 * DAY).toISOString());
  });

  it("reads the last 7 days", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    const { server } = renderRuns({ status: 200, body: [] }, "?period=7d");

    await screen.findByText("No run of this job in this period.");
    expect(queries(server.sent)[0]?.get("since")).toBe(new Date(NOW - 7 * DAY).toISOString());
  });

  it("shows more by cursor and stops reading the first page again once more were shown", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server } = renderRuns((request) =>
      request.path.includes("cursor=")
        ? { status: 200, body: [aJobRun({ id: "old", created_at: "2026-10-01T01:00:00Z" })] }
        : {
            status: 200,
            body: [aJobRun({ id: "r2", tasks: [] })],
            headers: { "X-Next-Cursor": "c2" },
          },
    );
    await settle();
    expect(server.sent).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await settle();
    expect(server.sent).toHaveLength(2);
    expect(queries(server.sent)[1]?.has("cursor")).toBe(false);

    act(() => {
      screen.getByRole("button", { name: "Show more" }).click();
    });
    await settle();
    expect(server.sent).toHaveLength(3);
    expect(queries(server.sent)[2]?.get("cursor")).toBe("c2");
    expect(screen.getAllByRole("row")).toHaveLength(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    await settle();
    expect(server.sent).toHaveLength(3);
    expect(vi.getTimerCount()).toBe(0);
    expect(server.sent.map((request) => request.headers["x-lakana-background"])).toEqual([
      "1",
      "1",
      undefined,
    ]);
  });

  it("reads the first page at once for a new revision, but keeps the pages once more were shown", async () => {
    const { server, change } = renderRuns((request) =>
      request.path.includes("cursor=")
        ? { status: 200, body: [aJobRun({ id: "old", created_at: "2026-10-01T01:00:00Z" })] }
        : {
            status: 200,
            body: [aJobRun({ id: "r2", status: "running", tasks: [] })],
            headers: { "X-Next-Cursor": "c2" },
          },
    );
    await screen.findByRole("button", { name: "Show more" });

    change(1, null);
    await waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });

    screen.getByRole("button", { name: "Show more" }).click();
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(3);
    });
    const link = screen.getByRole("link", { name: when(aJobRun().created_at) });
    link.focus();

    change(2, aJobRun({ id: "r2", status: "failed", failed_tasks: 1, total_tasks: 1 }));
    await settle();
    expect(server.sent).toHaveLength(3);
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(link.closest("tr")).toHaveTextContent("Failed");
    expect(link.closest("tr")).toHaveTextContent("1 of 1 tasks failed");
    expect(link).toHaveFocus();
  });

  it("keeps the rows and a still Show more through a failed refresh, and says so after three", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let reads = 0;
    let open: (() => void) | null = null;
    const { server } = renderRuns(async () => {
      reads += 1;
      if (reads === 1) {
        return {
          status: 200,
          body: [aJobRun({ id: "r2", tasks: [] })],
          headers: { "X-Next-Cursor": "c2" },
        };
      }
      if (reads === 2) {
        await new Promise<void>((resolve) => {
          open = resolve;
        });
      }
      return { status: 503, body: { type: "Unavailable", detail: "The server is busy." } };
    });
    await settle();
    const link = screen.getByRole("link", { name: when(aJobRun().created_at) });
    link.focus();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(reads).toBe(2);
    expect(screen.getByRole("button", { name: "Show more" })).not.toHaveAttribute("data-pending");
    act(() => {
      open?.();
    });
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(link).toHaveFocus();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_999);
    });
    await settle();
    expect(reads).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await settle();
    expect(reads).toBe(3);
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    await settle();
    expect(reads).toBe(4);
    expect(screen.getByRole("alert")).toHaveTextContent("The server is busy.");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(link).toHaveFocus();
    expect(server.sent).toHaveLength(4);
  });

  it("shows a third page after the second", async () => {
    const pages: Record<string, { id: string; next?: string }> = {
      "": { id: "r2", next: "c2" },
      c2: { id: "r1", next: "c3" },
      c3: { id: "r0" },
    };
    renderRuns((request) => {
      const cursor = new URL(request.path, "http://localhost").searchParams.get("cursor") ?? "";
      const page = pages[cursor] ?? { id: "none" };
      return {
        status: 200,
        body: [aJobRun({ id: page.id, tasks: [] })],
        ...(page.next === undefined ? {} : { headers: { "X-Next-Cursor": page.next } }),
      };
    });

    (await screen.findByRole("button", { name: "Show more" })).click();
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(3);
    });
    screen.getByRole("button", { name: "Show more" }).click();

    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(4);
    });
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("says why Show more failed under the rows and keeps them", async () => {
    renderRuns((request) =>
      request.path.includes("cursor=")
        ? { status: 503, body: { type: "Unavailable", detail: "The server is busy." } }
        : {
            status: 200,
            body: [aJobRun({ id: "r2", tasks: [] })],
            headers: { "X-Next-Cursor": "c2" },
          },
    );

    (await screen.findByRole("button", { name: "Show more" })).click();

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
  });

  it("goes on reading the first page after a Show more that failed", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server } = renderRuns((request) =>
      request.path.includes("cursor=")
        ? { status: 503, body: { type: "Unavailable", detail: "The server is busy." } }
        : {
            status: 200,
            body: [aJobRun({ id: "r2", tasks: [] })],
            headers: { "X-Next-Cursor": "c2" },
          },
    );
    await settle();
    act(() => {
      screen.getByRole("button", { name: "Show more" }).click();
    });
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("The server is busy.");
    // The poll, paused while Show more read, starts again at once and then every 30 s.
    expect(server.sent).toHaveLength(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await settle();
    expect(server.sent).toHaveLength(4);
    expect(queries(server.sent).map((query) => query.get("cursor"))).toEqual([
      null,
      "c2",
      null,
      null,
    ]);
  });

  it("starts a filter again from its first page, and drops a Show more of another filter", async () => {
    let release: (() => void) | null = null;
    const { server } = renderRuns(async (request) => {
      const query = new URL(request.path, "http://localhost").searchParams;
      if (query.has("cursor")) {
        if (query.get("cursor") === "slow") {
          await new Promise<void>((resolve) => {
            release = resolve;
          });
        }
        return {
          status: 200,
          body: [aJobRun({ id: `more-${query.get("cursor") ?? ""}`, tasks: [] })],
        };
      }
      const failed = query.get("status") === "failed";
      return {
        status: 200,
        body: [aJobRun({ id: failed ? "f1" : "r2", tasks: [] })],
        headers: { "X-Next-Cursor": failed ? "slow" : "c2" },
      };
    });
    const user = userEvent.setup();
    const choose = async (status: string): Promise<void> => {
      await user.click(screen.getByRole("button", { name: /Status/ }));
      await user.click(screen.getByRole("option", { name: status }));
    };

    // All: Show more reads a second page; Failed, then All again: its first page alone.
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(3);
    });
    await choose("Failed");
    await waitFor(() => {
      expect(screen.getByLabelText("where")).toHaveTextContent("?status=failed");
    });
    await choose("All");
    await waitFor(() => {
      expect(screen.getByLabelText("where")).toHaveTextContent(/^$/);
    });
    await screen.findByRole("button", { name: "Show more" });
    expect(screen.getAllByRole("row")).toHaveLength(2);

    // Failed: a Show more still on its way when the filter goes to All is dropped.
    await choose("Failed");
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(release).not.toBeNull();
    });
    await choose("All");
    act(() => {
      release?.();
    });
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    await choose("Failed");
    await waitFor(() => {
      expect(screen.getByLabelText("where")).toHaveTextContent("?status=failed");
    });
    await screen.findByRole("button", { name: "Show more" });
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(server.sent.filter((request) => request.path.includes("cursor="))).toHaveLength(2);
  });

  it("reads nothing and starts no timer in a tab that opens hidden", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => "hidden");
    const { server } = renderRuns({ status: 200, body: [aJobRun({ id: "r2", tasks: [] })] });
    await settle();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(server.sent).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
    vi.restoreAllMocks();
  });

  it("does not read again while the tab is hidden, and reads at once when it shows", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let visibility = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibility as DocumentVisibilityState,
    );
    const { server } = renderRuns({ status: 200, body: [aJobRun({ id: "r2", tasks: [] })] });
    await settle();

    visibility = "hidden";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    await settle();
    expect(server.sent).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);

    visibility = "visible";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await settle();
    expect(server.sent).toHaveLength(2);
    vi.restoreAllMocks();
  });
});
