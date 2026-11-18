import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readFilter, writeFilter } from "./remember";

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

describe("the remembered filter of the Connections list", () => {
  it("keeps the filter per person under lakana.connections.<person>", () => {
    const storage = new MemoryStorage();

    expect(readFilter("kim.lee", storage)).toBeNull();
    writeFilter("kim.lee", "prod", storage);

    expect(readFilter("kim.lee", storage)).toBe("prod");
    expect(readFilter("sam.ortiz", storage)).toBeNull();
    expect(storage.getItem("lakana.connections.kim.lee")).toBe("prod");
  });

  it("forgets the filter for all environments", () => {
    const storage = new MemoryStorage();
    writeFilter("kim.lee", "prod", storage);

    writeFilter("kim.lee", null, storage);

    expect(readFilter("kim.lee", storage)).toBeNull();
  });

  it("uses the browser's storage by default", () => {
    writeFilter("kim.lee", "dev");

    expect(readFilter("kim.lee")).toBe("dev");
  });

  it("ignores a storage that refuses, and a browser without one", () => {
    const refusing = new RefusingStorage();

    expect(readFilter("kim.lee", refusing)).toBeNull();
    expect(() => {
      writeFilter("kim.lee", "prod", refusing);
      writeFilter("kim.lee", null, refusing);
    }).not.toThrow();
    expect(readFilter("kim.lee", null)).toBeNull();
    expect(() => {
      writeFilter("kim.lee", "prod", null);
      writeFilter("kim.lee", null, null);
    }).not.toThrow();
  });
});
