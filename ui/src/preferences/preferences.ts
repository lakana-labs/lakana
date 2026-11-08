/**
 * Personal preferences (theme, density, language), kept in the browser's local storage.
 *
 * @packageDocumentation
 */

/** The colour theme: the system's, or one the user chose. */
export type Theme = "system" | "light" | "dark";

/** The density of lists: roomy by default, compact as a personal choice. */
export type Density = "roomy" | "compact";

/** The interface language; English is the only one so far. */
export type Language = "en";

/** Everything the user menu lets a person choose for themselves. */
export interface Preferences {
  /** The colour theme. */
  readonly theme: Theme;
  /** The density of lists. */
  readonly density: Density;
  /** The interface language. */
  readonly language: Language;
}

/** What a person gets before choosing anything, and per field whenever a stored value is bad. */
export const DEFAULT_PREFERENCES: Preferences = {
  theme: "system",
  density: "roomy",
  language: "en",
};

/** The local-storage key; `public/theme-boot.js` reads the same one. */
export const PREFERENCES_KEY = "lakana.preferences";

const THEMES: readonly Theme[] = ["system", "light", "dark"];
const DENSITIES: readonly Density[] = ["roomy", "compact"];
const LANGUAGES: readonly Language[] = ["en"];

/**
 * Picks a stored field when it is one of the allowed values.
 *
 * @param allowed - The values the field may take.
 * @param value - What was stored.
 * @param fallback - The default for the field.
 * @returns The stored value, or the default.
 */
function pick<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.find((candidate) => candidate === value) ?? fallback;
}

/**
 * The browser's local storage, or null where the browser blocks it (some private windows,
 * blocked site data), which throws on access.
 *
 * @returns The storage, or null.
 */
export function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Reads the stored preferences, falling back to the default per field: a value that is
 * missing, corrupt, from an older version or unreadable never crashes the interface.
 *
 * @param storage - Where the preferences are kept, or null when there is no storage.
 * @returns The preferences.
 */
export function readPreferences(storage: Storage | null): Preferences {
  let stored: unknown = null;
  try {
    stored = JSON.parse(storage?.getItem(PREFERENCES_KEY) ?? "null");
  } catch {
    // Corrupt or unreadable: every field takes its default.
  }
  const fields: Record<string, unknown> =
    typeof stored === "object" && stored !== null ? (stored as Record<string, unknown>) : {};
  return {
    theme: pick(THEMES, fields["theme"], DEFAULT_PREFERENCES.theme),
    density: pick(DENSITIES, fields["density"], DEFAULT_PREFERENCES.density),
    language: pick(LANGUAGES, fields["language"], DEFAULT_PREFERENCES.language),
  };
}

/**
 * Stores the preferences; where storage is missing or full, the choice lasts until the
 * page is reloaded.
 *
 * @param storage - Where to keep them, or null.
 * @param value - The preferences.
 */
export function writePreferences(storage: Storage | null, value: Preferences): void {
  try {
    storage?.setItem(PREFERENCES_KEY, JSON.stringify(value));
  } catch {
    // Blocked or full: the choice still applies to this page.
  }
}

/**
 * How long `data-theme-fade` stays on the root after a theme change: the 200 ms motion step of
 * `--motion-panel` in `tokens.css`, during which `base.css` fades the colours.
 */
export const THEME_FADE_MS = 200;

const fades = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>();

/**
 * Marks the root with `data-theme-fade` for {@link THEME_FADE_MS}, restarting a running fade.
 *
 * @param root - The `<html>` element.
 */
function fadeTheme(root: HTMLElement): void {
  clearTimeout(fades.get(root));
  root.setAttribute("data-theme-fade", "");
  fades.set(
    root,
    setTimeout(() => {
      root.removeAttribute("data-theme-fade");
    }, THEME_FADE_MS),
  );
}

/**
 * Applies the preferences to the document root: `data-theme` only for a chosen light or dark
 * theme (the system theme leaves it to `prefers-color-scheme`), `data-density` and `lang`. A
 * change of `data-theme` fades the colours; the first paint and an unchanged theme do not.
 *
 * @param root - The `<html>` element.
 * @param value - The preferences.
 */
export function applyPreferences(root: HTMLElement, value: Preferences): void {
  if (root.getAttribute("data-theme") !== (value.theme === "system" ? null : value.theme)) {
    fadeTheme(root);
  }
  if (value.theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", value.theme);
  }
  root.setAttribute("data-density", value.density);
  root.lang = value.language;
}
