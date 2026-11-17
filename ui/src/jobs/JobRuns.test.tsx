import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

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
 * @returns The fake server and the rows handler.
 */
function renderRuns(route: FakeRoute, search = "") {
  const server = fakeServer({ [RUNS]: route });
  const onRows = vi.fn();
  render(
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
                revision={0}
                onRows={onRows}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return { server, onRows };
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
    expect(within(rows[2] as HTMLElement).getAllByRole("cell")[2]).toHaveTextContent(/^$/);
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
  });

  it("starts no timer in a tab that opens hidden", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => "hidden");
    const { server } = renderRuns({ status: 200, body: [aJobRun({ id: "r2", tasks: [] })] });
    await settle();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(server.sent).toHaveLength(1);
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
