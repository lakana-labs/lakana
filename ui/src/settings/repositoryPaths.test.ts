import { describe, expect, it } from "vitest";

import { repositoryPath } from "./repositoryPaths";

describe("repositoryPath", () => {
  it.each([
    ["analytics", "/settings/repositories/analytics"],
    ["new", "/settings/repositories/new/about"],
    ["NEW", "/settings/repositories/NEW/about"],
    ["a b", "/settings/repositories/a%20b"],
  ])("gives %s its page's address", (name, path) => {
    expect(repositoryPath(name)).toBe(path);
  });
});
