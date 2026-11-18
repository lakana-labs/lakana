import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import { chooseEnvironment, secretUseText } from "./secretUse";

const i18n = initI18n();

describe("secretUseText", () => {
  it("names each use", () => {
    const t = i18n.t.bind(i18n);
    const of = (
      use: "package_index" | "repository" | "connection" | "environment" | null,
      target: string | null,
    ): string => secretUseText(t, { use, use_target: target, environment: "dev" });

    expect(of("package_index", "corp")).toBe("Package index corp");
    expect(of("repository", "analytics")).toBe("Repository analytics");
    expect(of("connection", "warehouse")).toBe("Connection warehouse");
    expect(of("environment", null)).toBe("Storage and webhooks of dev");
    expect(of(null, null)).toBe("no use yet");
    expect(of("package_index", null)).toBe("A package index that was removed");
    expect(of("repository", null)).toBe("A repository that was removed");
    expect(of("connection", null)).toBe("A connection that was removed");
  });

  it("marks a hidden character in a name", () => {
    const t = i18n.t.bind(i18n);
    expect(secretUseText(t, { use: "repository", use_target: "a‮b", environment: "dev" })).toBe(
      "Repository aU+202Eb",
    );
  });

  it("chooses the linked, else the remembered, else the first environment", () => {
    expect(chooseEnvironment(["dev", "prod"], "prod", "dev")).toEqual({
      allowed: ["dev", "prod"],
      chosen: "prod",
      refused: false,
    });
    expect(chooseEnvironment(["dev", "prod"], null, "prod").chosen).toBe("prod");
    expect(chooseEnvironment(["dev"], "prod", "prod")).toEqual({
      allowed: ["dev"],
      chosen: "dev",
      refused: true,
    });
    expect(chooseEnvironment(null, null, null)).toEqual({
      allowed: [],
      chosen: "",
      refused: false,
    });
  });
});
