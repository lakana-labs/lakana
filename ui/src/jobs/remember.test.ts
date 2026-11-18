import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readJobsFilter, writeJobsFilter } from "./remember";

describe("the Jobs list's remembered environment", () => {
  it("keeps the environment under lakana.jobs.environment, and forgets it for all", () => {
    const storage = new MemoryStorage();

    expect(readJobsFilter(storage)).toBeNull();
    writeJobsFilter("prod", storage);
    expect(storage.getItem("lakana.jobs.environment")).toBe("prod");
    expect(readJobsFilter(storage)).toBe("prod");
    writeJobsFilter(null, storage);
    expect(readJobsFilter(storage)).toBeNull();

    writeJobsFilter("dev");
    expect(readJobsFilter()).toBe("dev");
    window.localStorage.clear();
  });
});
