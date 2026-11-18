import { describe, expect, it } from "vitest";

import type { ManagedScopeOut } from "../api/accessFile";
import { managedScopeOf } from "./managed";

/**
 * A managed scope.
 *
 * @param scope - Its name.
 * @returns The scope.
 */
function managed(scope: string): ManagedScopeOut {
  return {
    scope,
    file_name: "access.yaml",
    file_sha256: "ab12",
    source_commit: null,
    applied_by: "admin",
    applied_at: "2026-11-18T09:00:00Z",
  };
}

const PROD = managed("prod");
const INSTANCE = managed("instance");

describe("managedScopeOf", () => {
  it.each<[readonly ManagedScopeOut[], string | null, ManagedScopeOut | null]>([
    [[], "dev", null],
    [[], null, null],
    [[PROD], "prod", PROD],
    [[PROD], "dev", null],
    [[PROD], null, null],
    [[INSTANCE], "dev", INSTANCE],
    [[INSTANCE], null, INSTANCE],
    [[INSTANCE, PROD], "prod", PROD],
  ])("follows the server's rule (%#)", (scopes, environment, expected) => {
    expect(managedScopeOf(scopes, environment)).toBe(expected);
  });
});
