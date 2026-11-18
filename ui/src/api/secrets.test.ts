import { describe, expect, it } from "vitest";

import { setStepUp } from "./client";
import { bindSecret, createSecret, listSecrets, revokeSecret, rotateSecret } from "./secrets";

import { mySecretEnvironments } from "./secretEnvironments";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

const SECRET = {
  id: "s1",
  name: "pypi-corp",
  environment: "dev",
  created_by: "p1",
  created_at: "2026-11-01T10:00:00Z",
  rotated_at: null,
  format_version: 2,
  key_id: "env:0123456789abcdef",
  use: "package_index",
  use_target: "corp",
  use_target_url: "https://pkgs.example.org/simple",
};

const calls = (server: ReturnType<typeof fakeServer>): string[] =>
  server.sent.map((request) => `${request.method} ${request.path}`);

describe("secrets", () => {
  it("writes with the passkey asked first, deletes without", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "GET /api/v1/environments/dev/secrets": { status: 200, body: [SECRET] },
      "POST /api/v1/environments/dev/secrets": { status: 201, body: SECRET },
      "PATCH /api/v1/environments/dev/secrets/pypi-corp": { status: 200, body: SECRET },
      "PUT /api/v1/environments/dev/secrets/pypi-corp/use": { status: 200, body: SECRET },
      "DELETE /api/v1/environments/dev/secrets/pypi-corp": { status: 204 },
    });
    setStepUp(() => Promise.resolve());

    await expect(listSecrets("dev")).resolves.toEqual([SECRET]);
    await createSecret("dev", { name: "pypi-corp", values: { password: "p" }, use: null });
    await createSecret("dev", {
      name: "pypi-corp",
      values: { password: "p" },
      use: { kind: "package_index", target: "corp" },
    });
    await rotateSecret("dev", "pypi-corp", { password: "q" });
    await bindSecret("dev", "pypi-corp", { kind: "environment", target: null });
    await revokeSecret("dev", "pypi-corp");

    expect(calls(server)).toEqual([
      "GET /api/v1/environments/dev/secrets",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/environments/dev/secrets",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/environments/dev/secrets",
      "GET /api/v1/sign-in/session",
      "PATCH /api/v1/environments/dev/secrets/pypi-corp",
      "GET /api/v1/sign-in/session",
      "PUT /api/v1/environments/dev/secrets/pypi-corp/use",
      "DELETE /api/v1/environments/dev/secrets/pypi-corp",
    ]);
    expect(server.sent[2]?.body).toEqual({ name: "pypi-corp", values: { password: "p" } });
    expect(server.sent[4]?.body).toEqual({
      name: "pypi-corp",
      values: { password: "p" },
      use: { kind: "package_index", target: "corp" },
    });
    expect(server.sent[8]?.body).toEqual({ kind: "environment", target: null });
  });

  it("sends the values only in the body", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/environments/a b/secrets": { status: 201, body: SECRET },
      "PATCH /api/v1/environments/a b/secrets/x/y": { status: 200, body: SECRET },
    });
    setStepUp(() => Promise.resolve());

    await createSecret("a b", { name: "x/y", values: { token: "s3cr3t-sentinel" }, use: null });
    await rotateSecret("a b", "x/y", { token: "s3cr3t-sentinel" });

    const writes = server.sent.filter((request) => request.method !== "GET");
    expect(writes.map((request) => request.path)).toEqual([
      "/api/v1/environments/a%20b/secrets",
      "/api/v1/environments/a%20b/secrets/x%2Fy",
    ]);
    for (const request of writes) {
      expect(request.path).not.toContain("s3cr3t-sentinel");
      expect(JSON.stringify(request.headers)).not.toContain("s3cr3t-sentinel");
      expect(JSON.stringify(request.body)).toContain("s3cr3t-sentinel");
    }
  });

  it("reads the environments where one manages secrets", async () => {
    const server = fakeServer({
      "GET /api/v1/whoami": { status: 200, body: { secret_environments: ["dev"] } },
    });

    await expect(mySecretEnvironments()).resolves.toEqual(["dev"]);
    server.route({ "GET /api/v1/whoami": { status: 200, body: { name: "kim" } } });
    await expect(mySecretEnvironments()).resolves.toEqual([]);
  });
});
