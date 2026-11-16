import { getConfig } from "@testing-library/react";
import { expect, test } from "vitest";

test("Testing Library waits 5 s for an element, so a slow first render under load still passes", () => {
  expect(getConfig().asyncUtilTimeout).toBe(5000);
});
