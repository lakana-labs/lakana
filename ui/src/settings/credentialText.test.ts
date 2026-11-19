import { describe, expect, it } from "vitest";

import type { RepositoryOut } from "../api";
import { initI18n } from "../i18n/i18n";
import { credentialText, lacksCredential } from "./credentialText";
import "./repositoryStrings";

const i18n = initI18n();

const SSH: RepositoryOut = {
  id: "r-1",
  name: "analytics",
  kind: "ssh",
  url: "git@github.com:acme/analytics.git",
  credential_secret: null,
  credential_username: null,
  ssh_known_hosts: null,
  default_ref: null,
  created_at: "2026-11-01T09:00:00Z",
  updated_at: "2026-11-01T09:00:00Z",
  deploy_keys: [],
};

const KEY = { environment: "dev", public_key: "ssh-ed25519 AAAA", fingerprint: "SHA256:x" };

describe("credentialText", () => {
  it.each<[string, RepositoryOut, string, boolean]>([
    ["deploy keys", { ...SSH, deploy_keys: [KEY] }, "Deploy key in 1 environment", false],
    ["an SSH key in a secret", { ...SSH, credential_secret: "k" }, "SSH key in secret k", false],
    ["a token", { ...SSH, kind: "https", credential_secret: "t" }, "Token in secret t", false],
    ["a local folder", { ...SSH, kind: "local" }, "None", false],
    ["nothing", SSH, "No credential", true],
  ])("names %s", (_, repo, words, lacks) => {
    expect(credentialText(i18n.t, repo)).toBe(words);
    expect(lacksCredential(repo)).toBe(lacks);
  });
});
