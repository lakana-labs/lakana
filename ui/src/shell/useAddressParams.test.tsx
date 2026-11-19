import { act, render, renderHook } from "@testing-library/react";
import { type ReactNode, useEffect } from "react";
import {
  createMemoryRouter,
  MemoryRouter,
  type NavigateFunction,
  RouterProvider,
  useLocation,
  useNavigate,
} from "react-router";
import { describe, expect, it } from "vitest";

import { settingParam, type UpdateParams, useAddressParams } from "./useAddressParams";

/** What the data router's screen last rendered with. */
let shown: { update: UpdateParams; navigate: NavigateFunction } | null = null;

/**
 * A screen of the data router that hands out its change and its navigation.
 *
 * @returns Nothing visible.
 */
function Screen(): null {
  const [, update] = useAddressParams();
  const navigate = useNavigate();
  useEffect(() => {
    shown = { update, navigate };
  });
  return null;
}

/**
 * Renders {@link Screen} in a data router, as the interface does, opened at an address.
 *
 * @param path - The address.
 * @returns The router.
 */
function renderDataRouter(path: string) {
  const router = createMemoryRouter([{ path: "*", element: <Screen /> }], {
    initialEntries: [path],
  });
  render(<RouterProvider router={router} />);
  return router;
}

/**
 * The screen's last change and navigation.
 *
 * @returns Them.
 */
function screenNow(): { update: UpdateParams; navigate: NavigateFunction } {
  if (shown === null) {
    throw new Error("the screen did not render");
  }
  return shown;
}

/**
 * Renders two users of the address's parameters side by side, as a screen's filters are.
 *
 * @param path - The address.
 * @returns The hook's result.
 */
function renderBoth(path: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={["/home", path]} initialIndex={1}>
      {children}
    </MemoryRouter>
  );
  return renderHook(
    () => ({
      one: useAddressParams(),
      two: useAddressParams(),
      location: useLocation(),
      navigate: useNavigate(),
    }),
    { wrapper },
  );
}

/**
 * A change that sets one parameter.
 *
 * @param name - The parameter.
 * @param value - Its value.
 * @returns The change.
 */
function setting(name: string, value: string) {
  return (current: URLSearchParams): URLSearchParams => {
    current.set(name, value);
    return current;
  };
}

describe("useAddressParams", () => {
  it("reads the address's parameters", () => {
    const { result } = renderBoth("/jobs/runs?period=7d");

    expect(result.current.one[0].get("period")).toBe("7d");
  });

  it("two changes before a render both reach the address, from one user or two", () => {
    const { result } = renderBoth("/jobs/runs?trigger=schedule");
    // Both changes come from the handlers of the same render, as two quick choices do.
    const [, first] = result.current.one;
    const [, second] = result.current.two;

    act(() => {
      first(setting("period", "7d"));
      second(setting("job", "nightly"));
    });
    expect(result.current.location.search).toBe("?trigger=schedule&period=7d&job=nightly");

    act(() => {
      const [, update] = result.current.one;
      update(setting("status", "failed"));
      update(setting("period", "24h"));
    });
    expect(result.current.location.search).toBe(
      "?trigger=schedule&period=24h&job=nightly&status=failed",
    );
  });

  it("a change after a render builds on that render's address, and keeps the options", () => {
    const { result } = renderBoth("/jobs/runs");

    act(() => {
      result.current.one[1](setting("period", "7d"));
    });
    // Back leaves the first change: the next builds on the address Back returned to.
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.search).toBe("");
    act(() => {
      result.current.two[1](setting("job", "nightly"), { replace: true });
    });
    expect(result.current.location.search).toBe("?job=nightly");
    // The change replaced the address, so Back leaves the page.
    act(() => {
      void result.current.navigate(-1);
    });
    expect(result.current.location.pathname).toBe("/home");
  });

  it("Back before the change renders forgets it, though Back gives the same location again", () => {
    const { result } = renderHook(
      () => ({ params: useAddressParams(), location: useLocation(), navigate: useNavigate() }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <MemoryRouter initialEntries={["/jobs/runs"]}>{children}</MemoryRouter>
        ),
      },
    );
    const [, update] = result.current.params;
    const { navigate } = result.current;

    act(() => {
      update(setting("period", "7d"));
      void navigate(-1);
    });
    expect(result.current.location.search).toBe("");
    act(() => {
      result.current.params[1](setting("job", "nightly"));
    });
    expect(result.current.location.search).toBe("?job=nightly");
  });
});

describe("useAddressParams in a data router", () => {
  it("two changes before a render both reach the address", async () => {
    const router = renderDataRouter("/jobs/runs?trigger=schedule");
    const { update } = screenNow();

    await act(async () => {
      update(settingParam("period", "7d"));
      update(settingParam("job", "nightly"));
      await Promise.resolve();
    });
    expect(router.state.location.search).toBe("?trigger=schedule&period=7d&job=nightly");
    // Each was a step of its own.
    await act(async () => {
      await screenNow().navigate(-1);
    });
    expect(router.state.location.search).toBe("?trigger=schedule&period=7d");
  });

  it("Back before the change renders forgets it", async () => {
    const router = renderDataRouter("/jobs/runs");
    const { update, navigate } = screenNow();

    await act(async () => {
      update(settingParam("period", "7d"));
      await navigate(-1);
    });
    expect(router.state.location.search).toBe("");
    await act(async () => {
      screenNow().update(settingParam("job", "nightly"));
      await Promise.resolve();
    });
    expect(router.state.location.search).toBe("?job=nightly");
  });

  it("a change made after the last one landed but before it rendered builds on it", async () => {
    const router = renderDataRouter("/jobs/runs");
    const { update } = screenNow();

    await act(async () => {
      update(settingParam("period", "7d"));
      // The router has the address; the screen has not rendered it.
      await new Promise((resolve) => setTimeout(resolve, 0));
      update(settingParam("job", "nightly"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(router.state.location.search).toBe("?period=7d&job=nightly");
  });
});
