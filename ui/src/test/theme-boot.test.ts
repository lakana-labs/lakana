import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { PREFERENCES_KEY } from "../preferences/preferences";

// The boot script lives in public/ (copied into the build as is), so its test lives here.
const SOURCE = readFileSync(resolve(import.meta.dirname, "../../public/theme-boot.js"), "utf8");

/** Runs the boot script against the current document, as the browser does in `<head>`. */
function boot(): void {
  // eslint-disable-next-line @typescript-eslint/no-implied-eval -- the script under test, read from disk
  (new Function(SOURCE) as () => void)();
}

const root = document.documentElement;

afterEach(() => {
  window.localStorage.clear();
  root.removeAttribute("data-theme");
  root.removeAttribute("data-density");
});

describe("theme-boot.js", () => {
  it("reads the same key as the interface", () => {
    expect(SOURCE).toContain(`"${PREFERENCES_KEY}"`);
  });

  it("sets data-theme and data-density before React from stored light/dark, ignores anything else", () => {
    window.localStorage.setItem(PREFERENCES_KEY, '{"theme":"dark","density":"compact"}');
    boot();
    expect(root.getAttribute("data-theme")).toBe("dark");
    expect(root.getAttribute("data-density")).toBe("compact");

    root.removeAttribute("data-theme");
    root.removeAttribute("data-density");
    window.localStorage.setItem(PREFERENCES_KEY, '{"theme":"light","density":"roomy"}');
    boot();
    expect(root.getAttribute("data-theme")).toBe("light");
    expect(root.getAttribute("data-density")).toBe("roomy");

    for (const stored of [
      '{"theme":"system"}',
      '{"theme":"neon","density":7}',
      "null",
      "{",
      "[]",
    ]) {
      root.removeAttribute("data-theme");
      root.removeAttribute("data-density");
      window.localStorage.setItem(PREFERENCES_KEY, stored);
      expect(() => {
        boot();
      }, stored).not.toThrow();
      expect(root.hasAttribute("data-theme"), stored).toBe(false);
      expect(root.hasAttribute("data-density"), stored).toBe(false);
    }
  });

  it("leaves the defaults with nothing stored", () => {
    boot();
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(root.hasAttribute("data-density")).toBe(false);
  });
});
