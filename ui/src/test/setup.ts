/**
 * Test setup that every Vitest file runs first: the DOM matchers of jest-dom, and a clean
 * document after each test.
 *
 * @packageDocumentation
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
