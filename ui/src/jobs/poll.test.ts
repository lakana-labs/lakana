import { expect, it } from "vitest";

import { LIST_EVERY_MS, LIVE_EVERY_MS } from "./poll";

it("reads a live run every 2 s and the lists every 30 s", () => {
  expect(LIVE_EVERY_MS).toBe(2_000);
  expect(LIST_EVERY_MS).toBe(30_000);
});
