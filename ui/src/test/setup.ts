/**
 * Test setup that every Vitest file runs first: the DOM matchers of jest-dom, object URLs
 * (which jsdom lacks), and a clean document, `fetch`, CSRF token, picture cache and local storage after
 * each test.
 *
 * @packageDocumentation
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { setCsrfToken } from "../api/client";
import { forgetPictures } from "../api/pictures";
import { initI18n } from "../i18n/i18n";

initI18n();

let objectUrls = 0;
// jsdom has no object URLs; a test spies on these to see what was made and revoked.
Object.assign(URL, {
  createObjectURL: () => {
    objectUrls += 1;
    return `blob:http://localhost/${String(objectUrls)}`;
  },
  revokeObjectURL: () => undefined,
});

afterEach(() => {
  cleanup();
  forgetPictures();
  vi.unstubAllGlobals();
  setCsrfToken(null);
  window.localStorage.clear();
});
