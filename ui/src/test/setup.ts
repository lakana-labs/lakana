/**
 * Test setup that every Vitest file runs first: the DOM matchers of jest-dom, and a clean
 * document, `fetch` and CSRF token after each test.
 *
 * @packageDocumentation
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { setCsrfToken } from "../api/client";
import { initI18n } from "../i18n/i18n";

initI18n();

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setCsrfToken(null);
});
