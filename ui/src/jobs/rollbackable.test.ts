import { describe, expect, it } from "vitest";

import type { DeployOut } from "../api/jobs";
import { aDeploy, aDeployment, DEPLOY_VERSION } from "../test/jobs";
import { mayRollBackTo } from "./rollbackable";

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
