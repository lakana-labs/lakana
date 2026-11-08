import { afterEach, describe, expect, it, vi } from "vitest";

import { MemoryStorage } from "../test/storage";
import {
  applyPreferences,
  browserStorage,
  DEFAULT_PREFERENCES,
  PREFERENCES_KEY,
  readPreferences,
  THEME_FADE_MS,
  writePreferences,
} from "./preferences";

/**
 * A storage that throws on every call, like a browser with blocked site data.
 *
 * @returns The storage.
 */
function throwingStorage(): Storage {
  const fail = (): never => {
    throw new DOMException("blocked", "SecurityError");
  };
  return {
    length: 0,
    clear: fail,
    getItem: fail,
    key: fail,
    removeItem: fail,
    setItem: fail,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("preferences", () => {
  it("reads the defaults from an empty storage", () => {
    expect(readPreferences(new MemoryStorage())).toEqual(DEFAULT_PREFERENCES);
    expect(DEFAULT_PREFERENCES).toEqual({ theme: "system", density: "roomy", language: "en" });
  });

  it("reads what was written", () => {
    const storage = new MemoryStorage();
    writePreferences(storage, { theme: "dark", density: "compact", language: "en" });

    expect(JSON.parse(storage.getItem(PREFERENCES_KEY) ?? "")).toEqual({
      theme: "dark",
      density: "compact",
      language: "en",
    });
    expect(readPreferences(storage)).toEqual({ theme: "dark", density: "compact", language: "en" });
  });

  it.each(["{", "null", '{"theme":"neon"}', '{"density":7}', "[]", '"dark"', '{"language":"tlh"}'])(
    "falls back per field for %s",
    (stored) => {
      const storage = new MemoryStorage();
      storage.setItem(PREFERENCES_KEY, stored);

      expect(readPreferences(storage)).toEqual(DEFAULT_PREFERENCES);
    },
  );

  it("keeps the valid fields of a partly corrupt value", () => {
    const storage = new MemoryStorage();
    storage.setItem(PREFERENCES_KEY, '{"theme":"light","density":"tiny","extra":1}');

    expect(readPreferences(storage)).toEqual({ ...DEFAULT_PREFERENCES, theme: "light" });
  });

  it("falls back when storage throws or is null", () => {
    expect(readPreferences(throwingStorage())).toEqual(DEFAULT_PREFERENCES);
    expect(readPreferences(null)).toEqual(DEFAULT_PREFERENCES);
    expect(() => {
      writePreferences(throwingStorage(), DEFAULT_PREFERENCES);
    }).not.toThrow();
    expect(() => {
      writePreferences(null, DEFAULT_PREFERENCES);
    }).not.toThrow();
  });

  it("finds the browser's storage, or none when it is blocked", () => {
    expect(browserStorage()).toBe(window.localStorage);

    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(browserStorage()).toBeNull();
  });

  it("system theme leaves data-theme unset", () => {
    const root = document.createElement("html");
    root.setAttribute("data-theme", "dark");

    applyPreferences(root, DEFAULT_PREFERENCES);

    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(root.getAttribute("data-density")).toBe("roomy");
    expect(root.lang).toBe("en");
  });

  it.each(["light", "dark"] as const)("a %s theme sets data-theme", (theme) => {
    const root = document.createElement("html");

    applyPreferences(root, { theme, density: "compact", language: "en" });

    expect(root.getAttribute("data-theme")).toBe(theme);
    expect(root.getAttribute("data-density")).toBe("compact");
  });

  it("fades the colours while the theme changes, and only then", () => {
    vi.useFakeTimers();
    try {
      const root = document.createElement("html");
      root.setAttribute("data-theme", "dark");

      applyPreferences(root, { theme: "light", density: "roomy", language: "en" });
      expect(root.hasAttribute("data-theme-fade")).toBe(true);

      vi.advanceTimersByTime(THEME_FADE_MS - 1);
      applyPreferences(root, { theme: "dark", density: "roomy", language: "en" });
      vi.advanceTimersByTime(THEME_FADE_MS - 1);
      // A second change restarts the fade rather than cutting it short.
      expect(root.hasAttribute("data-theme-fade")).toBe(true);

      vi.advanceTimersByTime(1);
      expect(root.hasAttribute("data-theme-fade")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["the same theme", "dark", "dark"],
    ["the system theme, still unset", null, "system"],
  ] as const)("does not fade on %s", (_case, before, theme) => {
    const root = document.createElement("html");
    if (before !== null) {
      root.setAttribute("data-theme", before);
    }

    applyPreferences(root, { theme, density: "compact", language: "en" });

    expect(root.hasAttribute("data-theme-fade")).toBe(false);
  });
});
