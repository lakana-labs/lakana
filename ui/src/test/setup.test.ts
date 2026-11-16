import { getConfig } from "@testing-library/react";
import { expect, test } from "vitest";

test("Testing Library waits 3 s for an element, below the 5 s test timeout, so a slow first render passes and a missing element still fails with its own message", () => {
  expect(getConfig().asyncUtilTimeout).toBe(3000);
});
