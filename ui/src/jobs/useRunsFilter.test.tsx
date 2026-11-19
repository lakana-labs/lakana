import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EnvironmentChoice } from "../api/environments";
import { toAllRunsFilter, useRunsFilter } from "./useRunsFilter";

const LISTED: readonly EnvironmentChoice[] = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/**
 * Renders the filter at an address, with the router's location beside it.
 *
 * @param path - The address.
 * @returns The hook's result.
 */
function renderFilter(path: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
  );
  return renderHook(
    () => ({ filter: useRunsFilter(LISTED), location: useLocation(), navigate: useNavigate() }),
    { wrapper },
  );
}

afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe("useRunsFilter", () => {
  it("reads every filter from the address, last 30 days by default", () => {
    const all = renderFilter("/jobs/runs").result.current.filter;
    expect(all.choice).toEqual({
      environment: null,
      status: "all",
      trigger: "all",
      job: "",
      period: "30d",
    });
    expect(all.narrowed).toBe(false);

    const some = renderFilter(
      "/jobs/runs?environment=prod&status=failed&trigger=manual&job=%20nightly_sales%20&period=7d",
    ).result.current.filter;
    expect(some.choice).toEqual({
      environment: "prod",
      status: "failed",
      trigger: "manual",
      job: "nightly_sales",
      period: "7d",
    });
    expect(some.narrowed).toBe(true);

    // What the address holds and the tab does not know is the default.
    expect(
      renderFilter("/jobs/runs?status=lost&trigger=cron&period=1y").result.current.filter.choice,
    ).toMatchObject({ status: "all", trigger: "all", period: "30d" });
  });

  it("queued or running is one choice of two statuses", () => {
    vi.useFakeTimers({ now: new Date("2026-11-18T12:00:00Z"), toFake: ["Date"] });
    const { result } = renderFilter("/jobs/runs?status=queued%2Crunning&trigger=deploy&job=daily");

    expect(result.current.filter.choice.status).toBe("queued,running");
    expect(toAllRunsFilter(result.current.filter.choice)).toEqual({
      environment: null,
      status: ["queued", "running"],
      trigger: ["deploy"],
      job: "daily",
      repository: null,
      since: "2026-10-19T12:00:00.000Z",
    });
    expect(
      toAllRunsFilter({
        ...result.current.filter.choice,
        status: "all",
        trigger: "all",
        job: "",
        period: "all",
      }),
    ).toMatchObject({ status: [], trigger: [], job: null, since: null });
  });

  it("writes a change to the address, a default as no parameter", () => {
    const { result } = renderFilter("/jobs/runs?status=failed");

    act(() => {
      result.current.filter.set({ status: "queued,running", job: "  nightly_sales " });
    });
    expect(result.current.location.search).toBe("?status=queued%2Crunning&job=nightly_sales");

    act(() => {
      result.current.filter.set({ status: "all", period: "24h" });
    });
    expect(result.current.location.search).toBe("?job=nightly_sales&period=24h");
  });

  it("a choice of environment writes the shared filter", () => {
    const { result } = renderFilter("/jobs/runs?status=failed");

    act(() => {
      result.current.filter.set({ environment: "dev" });
    });
    expect(result.current.filter.choice.environment).toBe("dev");
    expect(result.current.location.search).toBe("?status=failed&environment=dev");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("dev");
  });

  it("clear keeps the environment", () => {
    const { result } = renderFilter(
      "/jobs/runs?environment=prod&status=failed&trigger=api&job=daily&period=all",
    );

    act(() => {
      result.current.filter.clear();
    });
    expect(result.current.location.search).toBe("?environment=prod");
    expect(result.current.filter.narrowed).toBe(false);
  });

  it("each change is a step of the history, so Back undoes one filter at a time", () => {
    const { result } = renderFilter("/jobs/runs");

    act(() => {
      result.current.filter.set({ status: "failed" });
    });
    act(() => {
      result.current.filter.set({ environment: "prod" });
    });
    act(() => {
      result.current.filter.set({ trigger: "manual" });
    });
    expect(result.current.location.search).toBe("?status=failed&environment=prod&trigger=manual");

    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.pathname).toBe("/jobs/runs");
    expect(result.current.location.search).toBe("?status=failed&environment=prod");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("?status=failed");
    expect(result.current.filter.choice.status).toBe("failed");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("");
  });
});
