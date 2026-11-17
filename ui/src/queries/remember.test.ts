import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readEnvironment, readFilter, writeEnvironment, writeFilter } from "./remember";

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

describe("the remembered choices of the Queries screen", () => {
  it("keeps the environment under lakana.queries.environment", () => {
    const storage = new MemoryStorage();

    expect(readEnvironment(storage)).toBeNull();
    writeEnvironment("prod", storage);

    expect(readEnvironment(storage)).toBe("prod");
    expect(storage.getItem("lakana.queries.environment")).toBe("prod");
  });

  it("keeps the filter under lakana.queries.filter, and all environments as none", () => {
    const storage = new MemoryStorage();

    writeFilter("dev", storage);
    expect(readFilter(storage)).toBe("dev");
    expect(storage.getItem("lakana.queries.filter")).toBe("dev");

    writeFilter(null, storage);
    expect(readFilter(storage)).toBeNull();
    expect(storage.getItem("lakana.queries.filter")).toBeNull();
  });

  it("uses the browser's storage by default", () => {
    writeEnvironment("dev");
    writeFilter("prod");

    expect(readEnvironment()).toBe("dev");
    expect(readFilter()).toBe("prod");
    expect(window.localStorage.getItem("lakana.queries.environment")).toBe("dev");
  });

  it("survives a storage that throws and a browser without storage", () => {
    const refusing = new RefusingStorage();

    expect(() => {
      writeEnvironment("prod", refusing);
      writeFilter("prod", refusing);
      writeFilter(null, refusing);
    }).not.toThrow();
    expect(readEnvironment(refusing)).toBeNull();
    expect(readFilter(refusing)).toBeNull();
    expect(() => {
      writeEnvironment("prod", null);
      writeFilter(null, null);
    }).not.toThrow();
    expect(readEnvironment(null)).toBeNull();
    expect(readFilter(null)).toBeNull();
  });
});
