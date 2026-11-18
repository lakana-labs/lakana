import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import type { EnvironmentChoice } from "../api/environments";
import { matchesSearch, useJobsFilter } from "./useJobsFilter";

const LISTED: readonly EnvironmentChoice[] = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/**
 * Renders the filter at an address, with the router's location beside it.
 *
 * @param path - The address.
 * @param listed - The listed environments; "reading" while they are read.
 * @returns The hook's result.
 */
function renderFilter(path: string, listed: readonly EnvironmentChoice[] | "reading" = LISTED) {
  const environments = listed === "reading" ? undefined : listed;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
  );
  return renderHook(() => ({ filter: useJobsFilter(environments), location: useLocation() }), {
    wrapper,
  });
}

afterEach(() => {
  window.localStorage.clear();
});

describe("useJobsFilter", () => {
  it("takes the address, then the remembered environment, then all", () => {
    window.localStorage.setItem("lakana.jobs.environment", "dev");

    expect(renderFilter("/jobs?environment=prod").result.current.filter.environment).toBe("prod");
    expect(renderFilter("/jobs").result.current.filter.environment).toBe("dev");
    window.localStorage.clear();
    expect(renderFilter("/jobs").result.current.filter.environment).toBeNull();
    // Not listed in the address either: all of them, nothing named.
    expect(renderFilter("/jobs?environment=gone").result.current.filter.environment).toBeNull();
  });

  it("falls back to all environments when the remembered one is not listed", () => {
    window.localStorage.setItem("lakana.jobs.environment", "staging");

    expect(renderFilter("/jobs").result.current.filter.environment).toBeNull();
    // While the environments are read, nothing is chosen yet.
    window.localStorage.setItem("lakana.jobs.environment", "dev");
    expect(renderFilter("/jobs", "reading").result.current.filter.environment).toBeNull();
  });

  it("a choice writes the address and the storage", () => {
    const { result } = renderFilter("/jobs?q=sales");

    act(() => {
      result.current.filter.setEnvironment("prod");
    });
    expect(result.current.filter.environment).toBe("prod");
    expect(result.current.location.search).toBe("?q=sales&environment=prod");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("prod");

    act(() => {
      result.current.filter.setEnvironment(null);
    });
    expect(result.current.filter.environment).toBeNull();
    expect(result.current.location.search).toBe("?q=sales");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBeNull();
  });

  it("the search is in the address", () => {
    const { result } = renderFilter("/jobs?environment=dev&q=night");

    expect(result.current.filter.query).toBe("night");
    act(() => {
      result.current.filter.setQuery("weekly");
    });
    expect(result.current.filter.query).toBe("weekly");
    expect(result.current.location.search).toBe("?environment=dev&q=weekly");
    act(() => {
      result.current.filter.setQuery("");
    });
    expect(result.current.location.search).toBe("?environment=dev");
    expect(result.current.filter.query).toBe("");
  });
});

describe("matchesSearch", () => {
  it("matches a part of any name, in any case; an empty search matches all", () => {
    expect(matchesSearch("", "nightly_sales")).toBe(true);
    expect(matchesSearch("  ", null)).toBe(true);
    expect(matchesSearch("SALES", "nightly_sales", "analytics")).toBe(true);
    expect(matchesSearch("analy", null, "analytics")).toBe(true);
    expect(matchesSearch("weekly", "nightly_sales", null)).toBe(false);
  });
});
