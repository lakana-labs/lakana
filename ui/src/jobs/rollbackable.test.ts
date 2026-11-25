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

describe("currentDeploys", () => {
  const now = aDeployment().project_version_id;

  it("marks only the newest successful deploy or rollback of each repository at its version", () => {
    const rows = [
      aDeploy({ id: "dry", kind: "dry_run", project_version_id: null }),
      aDeploy({ id: "failed", status: "failed", project_version_id: null }),
      aDeploy({ id: "back", kind: "rollback", requested_ref: null, project_version_id: now }),
      aDeploy({ id: "middle", project_version_id: DEPLOY_VERSION }),
      aDeploy({ id: "first", project_version_id: now }),
      aDeploy({ id: "other", repository: "finance", project_version_id: "v-finance" }),
    ];
    const current = new Map([...CURRENT, ["finance", "v-finance"]]);
    expect(currentDeploys(rows, current)).toEqual(new Set(["back", "other"]));
  });

  it("marks nothing when the newest successful run is not at the current version", () => {
    const rows = [aDeploy({ id: "newer" }), aDeploy({ id: "older", project_version_id: now })];
    expect(currentDeploys(rows, CURRENT)).toEqual(new Set());
    expect(currentDeploys(rows, new Map())).toEqual(new Set());
  });
});
