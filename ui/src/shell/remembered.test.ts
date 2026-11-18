import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readRemembered, writeRemembered } from "./remembered";

/** A storage that refuses every call, as a blocked browser does. */
class RefusingStorage extends MemoryStorage {
  override getItem(): string | null {
    throw new Error("blocked");
  }

  override setItem(): void {
    throw new Error("blocked");
  }

  override removeItem(): void {
    throw new Error("blocked");
  }
}

describe("remembered choices", () => {
  it("reads and writes a key", () => {
    const storage = new MemoryStorage();

    expect(readRemembered("lakana.jobs.environment", storage)).toBeNull();
    writeRemembered("lakana.jobs.environment", "prod", storage);
    expect(readRemembered("lakana.jobs.environment", storage)).toBe("prod");
    writeRemembered("lakana.jobs.environment", null, storage);
    expect(readRemembered("lakana.jobs.environment", storage)).toBeNull();

    writeRemembered("lakana.jobs.environment", "dev");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("dev");
    expect(readRemembered("lakana.jobs.environment")).toBe("dev");
    window.localStorage.clear();
  });

  it("a storage that throws reads as null and ignores the write", () => {
    const refusing = new RefusingStorage();

    expect(() => {
      writeRemembered("lakana.jobs.environment", "prod", refusing);
      writeRemembered("lakana.jobs.environment", null, refusing);
      writeRemembered("lakana.jobs.environment", "prod", null);
    }).not.toThrow();
    expect(readRemembered("lakana.jobs.environment", refusing)).toBeNull();
    expect(readRemembered("lakana.jobs.environment", null)).toBeNull();
  });
});
