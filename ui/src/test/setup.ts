/**
 * Test setup that every Vitest file runs first: the DOM matchers of jest-dom, object URLs,
 * `execCommand` and a range's geometry (which jsdom lacks), a 3 s wait for `findBy*` and
 * `waitFor`, and a clean document, `fetch`, CSRF token, picture cache and local storage after
 * each test.
 *
 * @packageDocumentation
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { setCsrfToken } from "../api/client";
import { forgetPictures } from "../api/pictures";
import { initI18n } from "../i18n/i18n";

initI18n();

// A file's first render can take longer than the default 1000 ms when CI's coverage job is
// loaded (#657); a wait that is met returns at once, so a longer limit costs nothing.
configure({ asyncUtilTimeout: 3000 });

let objectUrls = 0;
// jsdom has no object URLs; a test spies on these to see what was made and revoked.
Object.assign(URL, {
  createObjectURL: () => {
    objectUrls += 1;
    return `blob:http://localhost/${String(objectUrls)}`;
  },
  revokeObjectURL: () => undefined,
});

// jsdom has no execCommand, which CodeMirror's focus() calls (the SQL editor's focusAt).
Object.assign(Document.prototype, { execCommand: () => false });

// jsdom lays nothing out and has no geometry for a range, which CodeMirror measures when it
// scrolls the cursor into view (the SQL editor's focusAt) in the next animation frame.
Object.assign(Range.prototype, {
  getClientRects: () => [],
  getBoundingClientRect: () => new DOMRect(),
});

afterEach(() => {
  cleanup();
  forgetPictures();
  vi.unstubAllGlobals();
  setCsrfToken(null);
  window.localStorage.clear();
});
