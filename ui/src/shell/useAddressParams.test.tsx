import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { describe, expect, it } from "vitest";

import { useAddressParams } from "./useAddressParams";

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
});
