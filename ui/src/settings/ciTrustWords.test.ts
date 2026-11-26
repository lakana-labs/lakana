import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import {
  addRulePath,
  ciTrustRulePath,
  CI_TRUST_PATH,
  conditionsHelpKey,
  EXPLAIN_PATH,
  GITHUB_ISSUER,
  GITLAB_ISSUER,
  issuerPreset,
  issuerWords,
  kindsWords,
  readAddRule,
  startingConditions,
} from "./ciTrustWords";
import "./ciTrustStrings";

const i18n = initI18n();
const t = i18n.t.bind(i18n);

describe("ciTrustWords", () => {
  it("names the addresses of the place", () => {
    expect(CI_TRUST_PATH).toBe("/settings/ci-trust");
    expect(EXPLAIN_PATH).toBe("/settings/ci-trust/explain");
    expect(ciTrustRulePath("7b0c/a")).toBe("/settings/ci-trust/7b0c%2Fa");
  });

  it("opens Add rule from an address, filled in from it", () => {
    const path = addRulePath({ repository: "ana lytics", environment: "dev", issuer: "github" });
    expect(path).toBe(
      "/settings/ci-trust?add=1&repository=ana+lytics&environment=dev&issuer=github",
    );
    const params = new URLSearchParams(path.split("?")[1]);
    expect(readAddRule(params)).toEqual({
      repository: "ana lytics",
      environment: "dev",
      issuer: "github",
    });
    expect(readAddRule(new URLSearchParams("add=1&issuer=bitbucket"))).toEqual({
      repository: null,
      environment: null,
      issuer: null,
    });
    expect(readAddRule(new URLSearchParams("repository=x"))).toBeNull();
  });

  it("names the issuers in words, a self-hosted one by its URL", () => {
    expect(issuerWords(t, GITHUB_ISSUER)).toBe("GitHub Actions");
    expect(issuerWords(t, GITLAB_ISSUER)).toBe("GitLab.com");
    expect(issuerWords(t, "https://git.corp.example")).toBe("https://git.corp.example");
    expect(issuerPreset(GITHUB_ISSUER)).toBe("github");
    expect(issuerPreset(GITLAB_ISSUER)).toBe("gitlab");
    expect(issuerPreset("https://git.corp.example")).toBe("other");
  });

  it("says what a rule allows", () => {
    expect(kindsWords(t, ["deploy"])).toBe("Deploy");
    expect(kindsWords(t, ["dry_run", "deploy"])).toBe("Deploy and Dry run");
    expect(kindsWords(t, [])).toBe("");
  });

  it("starts the claims a rule needs, by issuer", () => {
    expect(startingConditions("github")).toEqual([
      { name: "repository_id", value: "" },
      { name: "ref", value: "refs/heads/main" },
      { name: "event_name", value: "push" },
    ]);
    expect(startingConditions("gitlab")).toEqual([
      { name: "project_id", value: "" },
      { name: "ref", value: "main" },
      { name: "ref_protected", value: "true" },
    ]);
    expect(startingConditions("other")).toEqual(startingConditions("gitlab"));
    expect(conditionsHelpKey("github")).toBe("settings.ciTrust.dialog.conditionsHelpGithub");
    expect(conditionsHelpKey("other")).toBe("settings.ciTrust.dialog.conditionsHelpGitlab");
  });
});
