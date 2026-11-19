import { act, render } from "@testing-library/react";
import { useEffect } from "react";
import {
  createBrowserRouter,
  type NavigateFunction,
  RouterProvider,
  useNavigate,
} from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EnvironmentChoice } from "../api/environments";
import { settingParam, type UpdateParams, useAddressParams } from "../shell/useAddressParams";
import { type JobsFilter, useJobsFilter } from "./useJobsFilter";

const LISTED: readonly EnvironmentChoice[] = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/** What the pages last rendered with: the tab's filter (null while another page shows). */
const page: {
  tab: JobsFilter | null;
  update: UpdateParams | null;
  navigate: NavigateFunction | null;
  environments: readonly EnvironmentChoice[] | undefined;
} = { tab: null, update: null, navigate: null, environments: LISTED };

/**
 * Hands out the navigation, on every page.
 *
 * @returns Nothing visible.
 */
function Navigation(): null {
  const navigate = useNavigate();
  useEffect(() => {
    page.navigate = navigate;
  });
  return null;
}

/**
 * The Runs tab's filter, handed out while the tab shows.
 *
 * @returns Nothing visible.
 */
function Tab(): null {
  const tab = useJobsFilter(page.environments, "push");
  const [, update] = useAddressParams();
  useEffect(() => {
    page.tab = tab;
    page.update = update;
  });
  useEffect(
    () => () => {
      page.tab = null;
    },
    [],
  );
  return null;
}

/**
 * Lets the router land and React render.
 *
 * @returns Once they have.
 */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 20));

let unload: (() => void) | null = null;

/**
 * Loads the interface's kind of router at the browser's address, as a page load does.
 *
 * @returns Once the page has rendered.
 */
async function load(): Promise<void> {
  unload?.();
  const router = createBrowserRouter([
    {
      path: "/jobs/runs",
      element: (
        <>
          <Navigation />
          <Tab />
        </>
      ),
    },
    { path: "*", element: <Navigation /> },
  ]);
  const shown = render(<RouterProvider router={router} />);
  unload = () => {
    shown.unmount();
    router.dispose();
  };
  await act(settle);
}

/**
 * Runs a step and lets it land.
 *
 * @param run - The step.
 * @returns Once it landed.
 */
async function step(run: () => unknown): Promise<void> {
  await act(async () => {
    await run();
    await settle();
  });
}

/**
 * The browser's Back button.
 *
 * @returns Once Back landed.
 */
const back = (): Promise<void> =>
  step(() => {
    window.history.back();
  });

/**
 * What the pages hold.
 *
 * @param value - A value set once the page rendered.
 * @returns It.
 */
function given<T>(value: T | null): T {
  if (value === null) {
    throw new Error("the page did not render");
  }
  return value;
}

/**
 * Says how this page was loaded, as the browser's navigation timing does.
 *
 * @param type - The navigation's type.
 */
function loadedBy(type: NavigationTimingType): void {
  vi.spyOn(performance, "getEntriesByType").mockReturnValue([
    { type } as PerformanceNavigationTiming,
  ]);
}

afterEach(() => {
  unload?.();
  unload = null;
  window.localStorage.clear();
  page.environments = LISTED;
  vi.restoreAllMocks();
});

describe("the Runs tab's filter under the browser router", () => {
  it("two changes in one task both land, each a step; Back then a change is that change", async () => {
    window.history.replaceState(null, "", "/jobs/runs");
    await load();

    await step(() => {
      given(page.update)(settingParam("period", "7d"));
      given(page.update)(settingParam("job", "nightly"));
    });
    expect(window.location.search).toBe("?period=7d&job=nightly");
    await back();
    expect(window.location.search).toBe("?period=7d");
    await back();
    expect(window.location.search).toBe("");
    await step(() => {
      given(page.update)(settingParam("job", "daily"));
    });
    expect(window.location.search).toBe("?job=daily");
  });

  it("Back from a run's page to the bare first address does not fill it in", async () => {
    window.history.replaceState(null, "", "/jobs/runs");
    await load();

    await step(() => {
      given(page.tab).setEnvironment("prod");
    });
    expect(window.location.search).toBe("?environment=prod");
    await back();
    expect(given(page.tab).environment).toBeNull();
    await step(() => given(page.navigate)("/jobs/prod/runs/1"));
    expect(page.tab).toBeNull();
    await back();
    expect(window.location.search).toBe("");
    expect(given(page.tab).environment).toBeNull();
  });

  it("a change pushed while the environments are read: Back to the first address stays on all", async () => {
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    page.environments = undefined;
    window.history.replaceState(null, "", "/jobs/runs");
    await load();

    await step(() => {
      given(page.tab).setQuery("nightly");
    });
    page.environments = LISTED;
    await step(() => given(page.navigate)("/elsewhere"));
    await back();
    await back();
    expect(window.location.search).toBe("");
    expect(given(page.tab).environment).toBeNull();
  });

  it("a fresh load of a bare address fills in the remembered environment", async () => {
    loadedBy("navigate");
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    window.history.replaceState(null, "", "/jobs/runs");
    await load();

    expect(window.location.search).toBe("?environment=prod");
    expect(given(page.tab).environment).toBe("prod");
  });

  it("a reload of the bare first address shows it as it was", async () => {
    window.history.replaceState(null, "", "/jobs/runs");
    await load();
    await step(() => {
      given(page.tab).setEnvironment("prod");
    });
    await back();
    expect(window.location.search).toBe("");

    loadedBy("reload");
    await load();
    expect(window.location.search).toBe("");
    expect(given(page.tab).environment).toBeNull();
  });

  it("a browser without navigation timing takes a load of the first address as fresh", async () => {
    vi.spyOn(performance, "getEntriesByType").mockReturnValue([]);
    window.localStorage.setItem("lakana.jobs.environment", "prod");
    window.history.replaceState(null, "", "/jobs/runs");
    await load();
    expect(window.location.search).toBe("?environment=prod");

    unload?.();
    unload = null;
    Object.defineProperty(performance, "getEntriesByType", {
      configurable: true,
      value: undefined,
    });
    try {
      window.history.replaceState(null, "", "/jobs/runs");
      await load();
      expect(window.location.search).toBe("?environment=prod");
    } finally {
      Reflect.deleteProperty(performance, "getEntriesByType");
    }
  });
});
