import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readJobsFilter, writeJobsFilter } from "./remember";

describe("the Jobs list's remembered environment", () => {
  it("keeps the environment under lakana.jobs.environment, and all environments as .", () => {
    const storage = new MemoryStorage();

    expect(readJobsFilter(storage)).toBeNull();
    writeJobsFilter("prod", storage);
    expect(storage.getItem("lakana.jobs.environment")).toBe("prod");
    expect(readJobsFilter(storage)).toBe("prod");
    // All environments is a choice of its own, kept as ".", which no environment name may hold
    // (the access file reads it as the start of a namespace), and read back as null.
    writeJobsFilter(null, storage);
    expect(storage.getItem("lakana.jobs.environment")).toBe(".");
    expect(readJobsFilter(storage)).toBeNull();
    // An environment named all is itself.
    writeJobsFilter("all", storage);
    expect(readJobsFilter(storage)).toBe("all");

    writeJobsFilter("dev");
    expect(readJobsFilter()).toBe("dev");
    window.localStorage.clear();
  });
});
