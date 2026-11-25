import { describe, expect, it } from "vitest";

import type { DeployOut } from "../api/jobs";
import { aDeploy, aDeployment, DEPLOY_VERSION } from "../test/jobs";
import { currentDeploys, mayRollBackTo } from "./rollbackable";

const CURRENT = new Map([["analytics", aDeployment().project_version_id]]);

describe("mayRollBackTo", () => {
  it.each<[string, Partial<DeployOut>, boolean]>([
    ["the current deploy", { project_version_id: aDeployment().project_version_id }, false],
    ["a dry run", { kind: "dry_run" }, false],
    ["a failed deploy", { status: "failed" }, false],
    ["a queued deploy", { status: "queued", resolved_sha: null, project_version_id: null }, false],
    ["an older successful deploy", {}, true],
    ["an older successful rollback", { kind: "rollback", requested_ref: null }, true],
    ["the same older deploy for a reader who may not roll back", { actions: [] }, false],
  ])("%s", (_name, over, offered) => {
    expect(mayRollBackTo(aDeploy(over), CURRENT)).toBe(offered);
  });

  it("offers nothing without a commit, or for a repository with no current version", () => {
    expect(mayRollBackTo(aDeploy({ resolved_sha: null }), CURRENT)).toBe(false);
    expect(mayRollBackTo(aDeploy(), new Map())).toBe(false);
    expect(mayRollBackTo(aDeploy(), new Map([["analytics", DEPLOY_VERSION]]))).toBe(false);
  });
});

/**
 * A time on 17 November 2026, some hours after midnight.
 *
 * @param hour - The hour.
 * @returns The ISO time.
 */
const at = (hour: number): string => `2026-11-17T0${String(hour)}:00:00Z`;

describe("currentDeploys", () => {
  const now = aDeployment().project_version_id;

  it("reads the instant, not the text: a whole second is before a fraction of it", () => {
    const rows = [
      aDeploy({ id: "a", project_version_id: DEPLOY_VERSION, created_at: "2026-11-17T10:00:00Z" }),
      aDeploy({ id: "b", project_version_id: now, created_at: "2026-11-17T10:00:00.000005Z" }),
    ];
    // Compared as text, `…:00Z` would sort after `…:00.000005Z` and mark nothing.
    expect(currentDeploys(rows, CURRENT)).toEqual(new Set(["b"]));
  });

  it("breaks a tie of times by the larger id, as the newest-first list does, in any order", () => {
    const tied = "2026-11-17T10:00:00Z";
    const rows = [
      aDeploy({ id: "d-1", project_version_id: now, created_at: tied }),
      aDeploy({ id: "d-2", project_version_id: now, created_at: tied }),
    ];
    expect(currentDeploys(rows, CURRENT)).toEqual(new Set(["d-2"]));
    expect(currentDeploys([...rows].reverse(), CURRENT)).toEqual(new Set(["d-2"]));
  });

  it("marks only the newest successful deploy or rollback of each repository at its version", () => {
    const rows = [
      aDeploy({ id: "dry", kind: "dry_run", project_version_id: null, created_at: at(6) }),
      aDeploy({ id: "failed", status: "failed", project_version_id: null, created_at: at(5) }),
      aDeploy({
        id: "back",
        kind: "rollback",
        requested_ref: null,
        project_version_id: now,
        created_at: at(4),
      }),
      aDeploy({ id: "middle", project_version_id: DEPLOY_VERSION, created_at: at(3) }),
      aDeploy({ id: "first", project_version_id: now, created_at: at(2) }),
      aDeploy({
        id: "other",
        repository: "finance",
        project_version_id: "v-finance",
        created_at: at(1),
      }),
    ];
    const current = new Map([...CURRENT, ["finance", "v-finance"]]);
    expect(currentDeploys(rows, current)).toEqual(new Set(["back", "other"]));
  });

  it("marks nothing when the newest successful run is not at the current version", () => {
    const rows = [
      aDeploy({ id: "newer", created_at: at(2) }),
      aDeploy({ id: "older", project_version_id: now, created_at: at(1) }),
    ];
    expect(currentDeploys(rows, CURRENT)).toEqual(new Set());
    expect(currentDeploys(rows, new Map())).toEqual(new Set());
  });

  it("marks the newest by time whatever order the list is in", () => {
    const rows = [
      aDeploy({ id: "first", project_version_id: now, created_at: "2026-11-10T08:00:00Z" }),
      aDeploy({
        id: "middle",
        project_version_id: DEPLOY_VERSION,
        created_at: "2026-11-12T08:00:00Z",
      }),
      aDeploy({
        id: "back",
        kind: "rollback",
        project_version_id: now,
        created_at: "2026-11-14T08:00:00Z",
      }),
    ];
    expect(currentDeploys(rows, CURRENT)).toEqual(new Set(["back"]));
  });
});
