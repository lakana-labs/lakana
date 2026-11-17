import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { aListedRun, RUN_ID } from "../test/queries";
import { fakeServer } from "../test/server";
import { RecentList } from "./RecentList";

const RECENT = "GET /api/v1/queries";

/**
 * Renders the Recent list of `kim.lee`.
 *
 * @param filter - The environment the list is filtered by, or null.
 */
function renderRecent(filter: string | null): void {
  render(
    <MemoryRouter>
      <RecentList me="kim.lee" filter={filter} reloadKey={0} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe("RecentList", () => {
  it("lists my written queries of 30 days with glyph, word, time, duration and environment", async () => {
    vi.useFakeTimers({ now: new Date("2026-11-17T10:00:00Z"), shouldAdvanceTime: true });
    const server = fakeServer({
      [RECENT]: {
        status: 200,
        body: {
          items: [
            aListedRun({ status: "RUNNING", finished_at: null, environment: "dev" }),
            aListedRun({
              id: "1c2d3e4f-5061-4b72-8c93-a4b5c6d7e8f9",
              status: "FAILED",
              started_at: "2026-11-16T09:00:00Z",
              finished_at: "2026-11-16T09:00:12.5Z",
            }),
            aListedRun({
              id: "2d3e4f50-6172-4c83-9da4-b5c6d7e8f9a0",
              status: "QUEUED",
              started_at: null,
              finished_at: null,
            }),
          ],
          next_cursor: null,
        },
      },
    });
    renderRecent(null);

    const region = await screen.findByRole("region", { name: "Recent queries" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    const [running, failed, queued] = rows as [HTMLElement, HTMLElement, HTMLElement];
    const when = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
    const link = within(running).getByRole("link", {
      name: when.format(new Date("2026-11-16T09:00:00Z")),
    });
    expect(link).toHaveAttribute("href", `/queries/dev/runs/${RUN_ID}`);
    expect(link).toHaveClass("row-name");
    expect(within(running).getByText("Running")).toBeInTheDocument();
    expect(running.querySelector(".status-glyph")).not.toBeNull();
    expect(running).toHaveTextContent("Environment dev");
    expect(running.querySelector(".second")).toBeNull();
    expect(within(failed).getByText("Failed")).toBeInTheDocument();
    expect(within(failed).getByText("12.5 s")).toBeInTheDocument();
    expect(failed).toHaveTextContent("Environment prod");
    expect(within(queued).getByText("Queued")).toBeInTheDocument();
    expect(queued.querySelector(".second")).toBeNull();
    expect(within(failed).getByText("›")).toHaveAttribute("aria-hidden", "true");
    const asked = new URL(String(server.sent[0]?.path), "http://localhost").searchParams;
    expect(asked.get("submitter")).toBe("kim.lee");
    expect(asked.get("origin")).toBe("written");
    expect(asked.get("since")).toBe("2026-10-18T10:00:00.000Z");
    expect(asked.has("environment")).toBe(false);
  });

  it("asks with the remembered filter", async () => {
    const server = fakeServer({
      [RECENT]: { status: 200, body: { items: [], next_cursor: null } },
    });
    renderRecent("prod");

    expect(await screen.findByText("No queries in the last 30 days.")).toBeInTheDocument();
    const asked = new URL(String(server.sent[0]?.path), "http://localhost").searchParams;
    expect(asked.get("environment")).toBe("prod");
  });
});
