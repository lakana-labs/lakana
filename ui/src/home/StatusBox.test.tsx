import { act, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DETAIL, HEALTHY } from "../test/home";
import { renderShell } from "../test/render";

/**
 * Renders Home with one status answer.
 *
 * @param body - What `GET /api/v1/status` answers.
 * @returns The rendered shell.
 */
function withStatus(body: unknown): ReturnType<typeof renderShell> {
  return renderShell("/", { routes: { "GET /api/v1/status": { status: 200, body } } });
}

/**
 * The status box.
 *
 * @returns The region.
 */
async function box(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Status" });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("StatusBox", () => {
  it.each([
    ["healthy", [], "Healthy", "Everything works."],
    ["degraded", [], "Degraded", "Lakana works, but slower than usual."],
    ["down", [], "Down", "Lakana cannot get work done right now. Tell an administrator."],
  ])(
    "shows Healthy, Degraded and Down with glyph and word (%s)",
    async (state, causes, word, line) => {
      withStatus({ ...HEALTHY, state, causes });

      const region = await box();
      expect(await within(region).findByText(word)).toBeInTheDocument();
      expect(within(region).getByText(line)).toBeInTheDocument();
      const glyph = { healthy: "succeeded", degraded: "warning", down: "failed" }[state];
      expect(region.querySelector(`.status-glyph-${String(glyph)}`)).not.toBeNull();
    },
  );

  it("shows the bars and Details only to administrators", async () => {
    withStatus({ ...HEALTHY, workers_alive: 2, workers_total: 3, detail: DETAIL });

    const region = await box();
    const compute = await within(region).findByRole("meter", { name: "Compute" });
    expect(compute).toHaveAttribute("value", "1");
    expect(compute).toHaveAttribute("max", "4");
    expect(within(region).getByText("1 of 4 slots busy")).toBeInTheDocument();
    expect(within(region).getByRole("meter", { name: "Memory" })).toBeInTheDocument();
    expect(within(region).getByText("1.2 GiB of 8.0 GiB")).toBeInTheDocument();
    expect(within(region).getByText("2 alive, 1 down")).toBeInTheDocument();
    expect(within(region).getByRole("link", { name: "Details" })).toHaveAttribute(
      "href",
      "/settings/status",
    );
  });

  it("hides the bars from a person who may not manage the instance", async () => {
    withStatus(HEALTHY);

    const region = await box();
    await within(region).findByText("Healthy");
    expect(within(region).queryByRole("meter")).not.toBeInTheDocument();
    expect(within(region).queryByRole("link", { name: "Details" })).not.toBeInTheDocument();
  });

  it("says when no worker runs and when the memory limit is unknown", async () => {
    withStatus({
      state: "down",
      causes: ["no_worker"],
      workers_alive: 0,
      workers_total: 0,
      detail: {
        ...DETAIL,
        slots_busy: 0,
        slots_total: 0,
        memory_used_bytes: 0,
        memory_limit_bytes: null,
      },
    });

    const region = await box();
    expect(
      await within(region).findByText(
        "No worker is running, so queries and jobs wait until one starts.",
      ),
    ).toBeInTheDocument();
    expect(within(region).getByText("No worker is running.")).toBeInTheDocument();
    expect(within(region).getByText("0.0 GiB in use, limit unknown")).toBeInTheDocument();
    expect(within(region).getByText("0 alive")).toBeInTheDocument();
    expect(within(region).queryByRole("meter")).not.toBeInTheDocument();
  });

  it("says a cause to a reader only when it affects them", async () => {
    withStatus({ ...HEALTHY, state: "degraded", causes: ["worker_lost"], workers_alive: 1 });

    const region = await box();
    expect(
      await within(region).findByText("Queries are slower: 1 of 2 workers is down."),
    ).toBeInTheDocument();
    expect(
      within(region).queryByText("Lakana works, but slower than usual."),
    ).not.toBeInTheDocument();
  });

  it("names every cause an administrator may see", async () => {
    withStatus({
      state: "down",
      causes: ["database", "storage", "worker_lost", "memory_high"],
      workers_alive: 1,
      workers_total: 3,
      detail: DETAIL,
    });

    const region = await box();
    expect(await within(region).findByText(/The database cannot be reached/)).toBeInTheDocument();
    expect(within(region).getByText(/The object storage cannot be reached/)).toBeInTheDocument();
    expect(
      within(region).getByText("Queries are slower: 2 of 3 workers are down."),
    ).toBeInTheDocument();
    expect(within(region).getByText(/close to their memory limit/)).toBeInTheDocument();
  });

  it("says why the status could not be read", async () => {
    renderShell("/", {
      routes: {
        "GET /api/v1/status": { status: 503, body: { detail: "Busy, try again.", type: "Busy" } },
      },
    });

    expect(await within(await box()).findByRole("alert")).toHaveTextContent("Busy, try again.");
  });

  it("polls the status in the background every 30 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = withStatus(HEALTHY);
    await within(await box()).findByText("Healthy");
    const polls = (): number =>
      server.sent.filter((request) => request.path === "/api/v1/status").length;
    expect(polls()).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(29_000);
    });
    expect(polls()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(polls()).toBe(2);

    const sent = server.sent.filter((request) => request.path === "/api/v1/status");
    expect(sent.every((request) => request.headers["x-lakana-background"] === "1")).toBe(true);
  });

  it("does not poll while the page is hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = withStatus(HEALTHY);
    await within(await box()).findByText("Healthy");
    const hidden = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(server.sent.filter((request) => request.path === "/api/v1/status")).toHaveLength(1);
    hidden.mockRestore();
  });
});
