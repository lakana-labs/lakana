import { act, renderHook } from "@testing-library/react";
import { type ReactNode, useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import type { EnvironmentChoice } from "../api/environments";
import { type JobsFilter, matchesSearch, useJobsFilter } from "./useJobsFilter";

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
    // All environments is remembered as a choice of its own, so it sticks across the tabs.
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe(".");
  });

  it("a remembered all environments is all of them on the lists", () => {
    window.localStorage.setItem("lakana.jobs.environment", ".");

    expect(renderFilter("/jobs").result.current.filter.environment).toBeNull();
    expect(renderFilter("/jobs/deploys").result.current.filter.environment).toBeNull();
  });

  it("an environment really named all is remembered as itself, on the lists and the Runs tab", () => {
    const withAll: readonly EnvironmentChoice[] = [...LISTED, { name: "all", protected: false }];
    const { result } = renderFilter("/jobs", withAll);

    act(() => {
      result.current.filter.setEnvironment("all");
    });
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("all");
    expect(renderFilter("/jobs", withAll).result.current.filter.environment).toBe("all");
    const runs = renderRunsTab("/jobs/runs", withAll).result.current;
    expect(runs.location.search).toBe("?environment=all");
    expect(runs.filter?.environment).toBe("all");
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

  it.each([
    ["the list replaces the address, so Back leaves it", undefined, "/home", "prod"],
    ["the Runs tab pushes, so Back undoes the choice", "push" as const, "/jobs/runs", null],
  ])("%s", (_, history, back, after) => {
    const { result } = renderHook(
      () => ({
        filter: useJobsFilter(LISTED, history),
        location: useLocation(),
        navigate: useNavigate(),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <MemoryRouter initialEntries={["/home", "/jobs/runs"]} initialIndex={1}>
            {children}
          </MemoryRouter>
        ),
      },
    );

    act(() => {
      result.current.filter.setEnvironment("prod");
    });
    expect(result.current.location.search).toBe("?environment=prod");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.pathname).toBe(back);
    expect(result.current.filter.environment).toBe(after);
  });

  it("on the Runs tab, after it opened, the address alone holds the environment: none is all", () => {
    window.localStorage.setItem("lakana.jobs.environment", "dev");
    const { result } = renderHook(
      () => ({
        filter: useJobsFilter(LISTED, "push"),
        location: useLocation(),
        navigate: useNavigate(),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <MemoryRouter
            initialEntries={["/jobs/runs", "/jobs/runs?environment=prod"]}
            initialIndex={1}
          >
            {children}
          </MemoryRouter>
        ),
      },
    );
    expect(result.current.filter.environment).toBe("prod");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.filter.environment).toBeNull();
    act(() => {
      void result.current.navigate(1);
    });

    act(() => {
      result.current.filter.setEnvironment(null);
    });
    expect(result.current.location.search).toBe("");
    expect(result.current.filter.environment).toBeNull();
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe(".");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.filter.environment).toBe("prod");
  });

  /**
   * Renders the Runs tab's filter on its route, so that it mounts when the tab is reached and
   * unmounts when another page is.
   *
   * @param path - The tab's address.
   * @param listed - The listed environments; "reading" while they are read.
   * @param opened - "push" to reach the tab from `/home`, as a link does; "load" to open the
   *   router at it, as a fresh load of the page does.
   * @returns The tab's filter (null while another page shows), the location, the navigation,
   *   and how to hand the tab its environments.
   */
  function renderRunsTab(
    path: string,
    listed: readonly EnvironmentChoice[] | "reading" = LISTED,
    opened: "push" | "load" = "push",
  ) {
    let environments = listed === "reading" ? undefined : listed;
    let tab: JobsFilter | null = null;
    const Tab = (): null => {
      tab = useJobsFilter(environments, "push");
      useEffect(
        () => () => {
          tab = null;
        },
        [],
      );
      return null;
    };
    const hook = renderHook(() => ({ location: useLocation(), navigate: useNavigate() }), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <MemoryRouter initialEntries={[opened === "push" ? "/home" : path]}>
          {children}
          <Routes>
            <Route path="/jobs/runs" element={<Tab />} />
            <Route path="*" element={null} />
          </Routes>
        </MemoryRouter>
      ),
    });
    if (opened === "push") {
      act(() => {
        void hook.result.current.navigate(path);
      });
    }
    return {
      result: {
        get current() {
          return { ...hook.result.current, filter: tab };
        },
      },
      rerender: (next: readonly EnvironmentChoice[]) => {
        environments = next;
        hook.rerender();
      },
    };
  }

  it("the Runs tab opens on the remembered environment, replacing the address", () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    const { result, rerender } = renderRunsTab("/jobs/runs?status=failed", "reading");

    // While the environments are read, none is chosen and the address waits.
    expect(result.current.filter?.environment).toBeNull();
    expect(result.current.location.search).toBe("?status=failed");
    rerender(LISTED);
    expect(result.current.location.search).toBe("?status=failed&environment=prod");
    expect(result.current.filter?.environment).toBe("prod");
    // No entry was added: Back leaves the tab.
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.pathname).toBe("/home");
  });

  it.each([
    ["nothing remembered", null],
    ["all environments remembered", "."],
    ["a remembered environment no longer listed", "staging"],
  ])("the Runs tab opens on all environments with %s", (_, remembered) => {
    if (remembered !== null) {
      window.localStorage.setItem("lakana.jobs.environment", remembered);
    }
    const { result } = renderRunsTab("/jobs/runs");

    expect(result.current.location.search).toBe("");
    expect(result.current.filter?.environment).toBeNull();
  });

  it("the address's environment wins over the remembered one on the Runs tab", () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    const { result } = renderRunsTab("/jobs/runs?environment=dev");

    expect(result.current.location.search).toBe("?environment=dev");
    expect(result.current.filter?.environment).toBe("dev");
  });

  it("Back after a choice on the Runs tab restores the environment before it", () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    const { result } = renderRunsTab("/jobs/runs");

    expect(result.current.filter?.environment).toBe("prod");
    act(() => {
      result.current.filter?.setEnvironment("dev");
    });
    expect(result.current.location.search).toBe("?environment=dev");
    act(() => {
      result.current.filter?.setEnvironment(null);
    });
    expect(result.current.location.search).toBe("");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe(".");

    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.filter?.environment).toBe("dev");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("?environment=prod");
    expect(result.current.filter?.environment).toBe("prod");
  });

  it("Back to the tab's first address without an environment stays on all", () => {
    const { result } = renderRunsTab("/jobs/runs");

    act(() => {
      result.current.filter?.setEnvironment("prod");
    });
    expect(result.current.filter?.environment).toBe("prod");
    // This browser now remembers prod, but the address Back returns to named none.
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("");
    expect(result.current.filter?.environment).toBeNull();
  });
  it("Back to the tab from another page does not fill in the environment chosen since", () => {
    const { result } = renderRunsTab("/jobs/runs");

    act(() => {
      result.current.filter?.setEnvironment("prod");
    });
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.filter?.environment).toBeNull();
    // A run, then Back: the tab mounts again on its first address, which named none.
    act(() => {
      void result.current.navigate("/jobs/prod/runs/1");
    });
    expect(result.current.filter).toBeNull();
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.pathname).toBe("/jobs/runs");
    expect(result.current.location.search).toBe("");
    expect(result.current.filter?.environment).toBeNull();
  });

  it("a change pushed while the environments are read leaves the first address as it was", () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    const { result, rerender } = renderRunsTab("/jobs/runs", "reading");

    act(() => {
      result.current.filter?.setQuery("nightly");
    });
    rerender(LISTED);
    expect(result.current.location.search).toBe("?q=nightly");
    // The address the tab opened at has been left: Back to it is all environments.
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("");
    expect(result.current.filter?.environment).toBeNull();
  });

  it("a fresh load of the tab fills in; Back to that first address later does not", () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    const loaded = renderRunsTab("/jobs/runs", LISTED, "load");
    expect(loaded.result.current.location.search).toBe("?environment=prod");
    expect(loaded.result.current.filter?.environment).toBe("prod");

    window.localStorage.clear();
    const { result } = renderRunsTab("/jobs/runs", LISTED, "load");
    expect(result.current.location.search).toBe("");
    act(() => {
      void result.current.navigate("/jobs/prod/runs/1");
    });
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("");
    expect(result.current.filter?.environment).toBeNull();
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
