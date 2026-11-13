import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/storage";
import { readChoice, writeChoice } from "./remember";

/** A storage that refuses every call, as a blocked browser does. */
class RefusingStorage extends MemoryStorage {
  override getItem(): string | null {
    throw new Error("blocked");
  }

  override setItem(): void {
    throw new Error("blocked");
  }
}

describe("remembered choice", () => {
  it("keeps the choice per person", () => {
    const storage = new MemoryStorage();

    writeChoice("kim.schulz", { environment: "prod", namespace: "gold" }, storage);

    expect(readChoice("kim.schulz", storage)).toEqual({ environment: "prod", namespace: "gold" });
    expect(readChoice("ana.nowak", storage)).toBeNull();
    expect(storage.getItem("lakana.catalog.kim.schulz")).not.toBeNull();
  });

  it("keeps an environment without a namespace and all tables", () => {
    const storage = new MemoryStorage();

    writeChoice("kim", { environment: "dev", namespace: null }, storage);
    expect(readChoice("kim", storage)).toEqual({ environment: "dev", namespace: null });
    writeChoice("kim", { environment: null, namespace: null }, storage);
    expect(readChoice("kim", storage)).toEqual({ environment: null, namespace: null });
  });

  it("uses the browser's storage by default", () => {
    writeChoice("kim", { environment: "prod", namespace: null });

    expect(readChoice("kim")).toEqual({ environment: "prod", namespace: null });
    expect(window.localStorage.getItem("lakana.catalog.kim")).toContain("prod");
  });

  it("reads nothing from text that is not a choice", () => {
    const storage = new MemoryStorage();
    for (const text of ["not json", "42", "null", '{"environment": 3}', '{"namespace": "x"}']) {
      storage.setItem("lakana.catalog.kim", text);

      expect(readChoice("kim", storage)).toBeNull();
    }
  });

  it("survives a storage that throws and a browser without storage", () => {
    const refusing = new RefusingStorage();

    expect(() => {
      writeChoice("kim", { environment: "prod", namespace: null }, refusing);
    }).not.toThrow();
    expect(readChoice("kim", refusing)).toBeNull();
    expect(() => {
      writeChoice("kim", { environment: "prod", namespace: null }, null);
    }).not.toThrow();
    expect(readChoice("kim", null)).toBeNull();
  });
});
