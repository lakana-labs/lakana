import { render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MemoryStorage } from "../test/storage";
import { PREFERENCES_KEY } from "./preferences";
import { PreferencesProvider, usePreferences } from "./PreferencesProvider";

const root = document.documentElement;

afterEach(() => {
  root.removeAttribute("data-theme");
  root.removeAttribute("data-density");
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("PreferencesProvider", () => {
  it("fades an operating-system theme change, and stops listening when it goes", () => {
    const media = new EventTarget() as MediaQueryList;
    const matchMedia = vi.fn(() => media);
    vi.stubGlobal("matchMedia", matchMedia);

    const { unmount } = render(
      <PreferencesProvider storage={new MemoryStorage()}>
        <p>child</p>
      </PreferencesProvider>,
    );
    expect(matchMedia).toHaveBeenCalledWith("(prefers-color-scheme: dark)");
    expect(root.hasAttribute("data-theme-fade")).toBe(false);

    media.dispatchEvent(new Event("change"));
    expect(root.hasAttribute("data-theme-fade")).toBe(true);
    root.removeAttribute("data-theme-fade");

    unmount();
    media.dispatchEvent(new Event("change"));
    expect(root.hasAttribute("data-theme-fade")).toBe(false);
  });

  it("applies the stored preferences to the page on start", () => {
    const storage = new MemoryStorage();
    storage.setItem(PREFERENCES_KEY, '{"theme":"dark","density":"compact"}');

    render(
      <PreferencesProvider storage={storage}>
        <p>child</p>
      </PreferencesProvider>,
    );

    expect(screen.getByText("child")).toBeInTheDocument();
    expect(root.getAttribute("data-theme")).toBe("dark");
    expect(root.getAttribute("data-density")).toBe("compact");
  });

  it("uses the browser's storage by default", () => {
    window.localStorage.setItem(PREFERENCES_KEY, '{"theme":"light"}');

    const { result } = renderHook(() => usePreferences(), {
      wrapper: ({ children }) => <PreferencesProvider>{children}</PreferencesProvider>,
    });

    expect(result.current[0].theme).toBe("light");
    expect(root.getAttribute("data-theme")).toBe("light");
  });

  it("refuses to be used outside the provider", () => {
    expect(() => renderHook(() => usePreferences())).toThrow(
      "usePreferences needs a PreferencesProvider above it",
    );
  });
});
