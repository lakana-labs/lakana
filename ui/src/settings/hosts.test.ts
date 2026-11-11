import { describe, expect, it } from "vitest";

import { httpsForm, queryOrFragment, recogniseHost, shellQuote } from "./hosts";

describe("recogniseHost", () => {
  it("turns a GitHub HTTPS URL into its SSH form and proposes the name", () => {
    expect(recogniseHost("https://github.com/acme/analytics.git")).toEqual({
      host: "github",
      sshUrl: "git@github.com:acme/analytics.git",
      name: "analytics",
      deployKeyPage: "https://github.com/acme/analytics/settings/keys/new",
      fingerprintsPage:
        "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints",
    });
  });

  it("reads the SSH forms of GitHub, GitLab and Bitbucket", () => {
    expect(recogniseHost("git@github.com:acme/analytics.git").sshUrl).toBe(
      "git@github.com:acme/analytics.git",
    );
    expect(recogniseHost("ssh://git@github.com/acme/analytics").host).toBe("github");
    const gitlab = recogniseHost("https://gitlab.com/acme/data/analytics");
    expect(gitlab).toMatchObject({
      host: "gitlab",
      sshUrl: "git@gitlab.com:acme/data/analytics.git",
      name: "analytics",
      deployKeyPage: "https://gitlab.com/acme/data/analytics/-/settings/repository",
    });
    expect(gitlab.fingerprintsPage).toContain("docs.gitlab.com");
    const bitbucket = recogniseHost("git@bitbucket.org:acme/analytics.git");
    expect(bitbucket).toMatchObject({
      host: "bitbucket",
      deployKeyPage: "https://bitbucket.org/acme/analytics/admin/access-keys/",
    });
    expect(bitbucket.fingerprintsPage).toContain("atlassian.com");
  });

  it("does not recognise a look-alike host", () => {
    for (const url of [
      "https://github.com.evil.example/acme/analytics.git",
      "https://evil.example/github.com/acme/analytics.git",
      "git@github.com.evil.example:acme/analytics.git",
      "https://github.com@evil.example/acme/analytics.git",
      "git@github.com?@evil.example:acme/analytics.git",
      "git@github.com#@evil.example:acme/analytics.git",
      "git@a@github.com:acme/analytics.git",
    ]) {
      const host = recogniseHost(url);
      expect(host.host, url).toBe("other");
      expect(host.deployKeyPage, url).toBeNull();
      expect(host.fingerprintsPage, url).toBeNull();
    }
  });

  it.each([
    ["https://github.com/acme/analytics/tree/main/models", "git@github.com:acme/analytics.git"],
    [
      "https://github.com/acme/analytics/blob/main/lakana.yaml",
      "git@github.com:acme/analytics.git",
    ],
    ["https://bitbucket.org/acme/analytics/src/main/", "git@bitbucket.org:acme/analytics.git"],
    [
      "https://gitlab.com/acme/data/analytics/-/tree/main",
      "git@gitlab.com:acme/data/analytics.git",
    ],
  ])("reduces the browser address %s to the repository", (url, ssh) => {
    expect(recogniseHost(url)).toMatchObject({ sshUrl: ssh, name: "analytics" });
  });

  it("recognises a host written in capitals or with a trailing dot", () => {
    for (const url of [
      "git@GitHub.com:acme/analytics.git",
      "ssh://git@GITHUB.COM/acme/analytics.git",
      "https://GitHub.com/acme/analytics",
      "git@github.com.:acme/analytics.git",
    ]) {
      expect(recogniseHost(url), url).toMatchObject({
        host: "github",
        sshUrl: "git@github.com:acme/analytics.git",
      });
    }
  });

  it("keeps the SSH URL of another host and has no links for it", () => {
    expect(recogniseHost("ssh://git@git.example.com:2222/team/analytics.git")).toEqual({
      host: "other",
      sshUrl: "ssh://git@git.example.com:2222/team/analytics.git",
      name: "analytics",
      deployKeyPage: null,
      fingerprintsPage: null,
    });
    expect(recogniseHost("git@git.example.com:team/analytics.git").sshUrl).toBe(
      "git@git.example.com:team/analytics.git",
    );
  });

  it("has no SSH form for an HTTPS URL of another host, or for what is not a URL", () => {
    expect(recogniseHost("https://git.example.com/team/analytics.git")).toMatchObject({
      host: "other",
      sshUrl: null,
      name: "analytics",
    });
    for (const url of ["", "analytics", "ftp://git.example.com/a", "https://github.com/"]) {
      expect(recogniseHost(url).sshUrl, url).toBeNull();
    }
    expect(recogniseHost("https://github.com/acme").deployKeyPage).toBeNull();
    expect(recogniseHost("https://gitlab.com/acme").deployKeyPage).toBeNull();
  });

  it("encodes the path parts of the links it builds", () => {
    expect(recogniseHost("https://github.com/acme/a%3Fb.git").deployKeyPage).toBe(
      "https://github.com/acme/a%3Fb/settings/keys/new",
    );
    expect(recogniseHost("https://github.com/acme/a%E0%A4%A").deployKeyPage).toBe(
      "https://github.com/acme/a%25E0%25A4%25A/settings/keys/new",
    );
  });
});

describe("queryOrFragment", () => {
  it("finds the '?' or '#' the server refuses in a repository URL", () => {
    expect(queryOrFragment("git@github.com:acme/a?b#c.git")).toBe(true);
    expect(queryOrFragment("https://github.com/acme/analytics#readme")).toBe(true);
    expect(queryOrFragment("https://github.com/acme/a%3Fb.git")).toBe(false);
    expect(recogniseHost("git@github.com:acme/a?b#c.git")).toMatchObject({
      host: "other",
      sshUrl: null,
      deployKeyPage: null,
    });
  });
});

describe("httpsForm", () => {
  it("is the URL itself for HTTPS, and the host and path of an SSH URL", () => {
    expect(httpsForm("https://git.example.com/team/analytics.git")).toBe(
      "https://git.example.com/team/analytics.git",
    );
    expect(httpsForm("git@github.com:acme/analytics.git")).toBe(
      "https://github.com/acme/analytics.git",
    );
    expect(httpsForm("ssh://git@git.example.com:2222/team/analytics.git")).toBe(
      "https://git.example.com/team/analytics.git",
    );
    expect(httpsForm("analytics")).toBeNull();
  });
});

describe("shellQuote", () => {
  it("quotes the way a POSIX shell reads it", () => {
    expect(shellQuote("analytics")).toBe("'analytics'");
    expect(shellQuote("it's $(rm -rf ~)")).toBe("'it'\\''s $(rm -rf ~)'");
  });
});
