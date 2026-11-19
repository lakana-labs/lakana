import { afterEach, describe, expect, it, vi } from "vitest";

import { setStepUp } from "./client";
import {
  createRepository,
  deleteRepository,
  generateDeployKey,
  getRepository,
  getRepositoryPage,
  listRepositories,
  makeMissingDeployKeys,
  replaceDeployKey,
  requestProbe,
  updateRepository,
  waitForProbe,
} from "./repositories";

import { fakeServer } from "../test/server";

const QUEUED = {
  id: "p-1",
  kind: "host_keys",
  state: "queued",
  outcome: null,
  message: null,
  host_keys: null,
  created_at: "2026-11-11T08:00:00Z",
  finished_at: null,
};
const DONE = { ...QUEUED, state: "done", outcome: "ok", message: "Found 1 key.", host_keys: [] };

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

const KEYS = { secret: "analytics-deploy-key", keys: [], skipped: [], replaced: [], notes: [] };

const calls = (server: ReturnType<typeof fakeServer>): string[] =>
  server.sent.map((request) => `${request.method} ${request.path}`);

afterEach(() => {
  vi.useRealTimers();
});

describe("repositories", () => {
  it("queues a check, then polls it every second in the background until it is done", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let polls = 0;
    const server = fakeServer({
      "POST /api/v1/repository-probes": { status: 202, body: QUEUED },
      "GET /api/v1/repository-probes/p-1": () => {
        polls += 1;
        return { status: 200, body: polls < 2 ? { ...QUEUED, state: "running" } : DONE };
      },
    });

    const probe = await requestProbe({ kind: "host_keys", url: "git@github.com:acme/a.git" });
    expect(probe.state).toBe("queued");
    const waiting = waitForProbe(probe.id, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(999);
    expect(polls).toBe(0);
    await vi.advanceTimersByTimeAsync(1001);

    expect(await waiting).toEqual(DONE);
    const gets = server.sent.filter((request) => request.method === "GET");
    expect(gets).toHaveLength(2);
    expect(gets.every((request) => request.headers["x-lakana-background"] === "1")).toBe(true);
    expect(server.sent[0]?.body).toEqual({ kind: "host_keys", url: "git@github.com:acme/a.git" });
  });

  it("stops polling when the page lets go", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = fakeServer({
      "GET /api/v1/repository-probes/p-1": { status: 200, body: QUEUED },
    });
    const leaving = new AbortController();

    const waiting = waitForProbe("p-1", leaving.signal);
    const refused = expect(waiting).rejects.toThrow("stopped");
    await vi.advanceTimersByTimeAsync(1000);
    leaving.abort();
    await refused;
    await vi.advanceTimersByTimeAsync(5000);

    expect(server.sent).toHaveLength(1);
    await expect(waitForProbe("p-1", leaving.signal)).rejects.toThrow("stopped");
  });

  it("registers an ssh repository and makes its keys", async () => {
    const out = { secret: "a-deploy-key", keys: [], skipped: [], replaced: [], notes: [] };
    const server = fakeServer({
      "POST /api/v1/repositories": { status: 201, body: { name: "a" } },
      "POST /api/v1/repositories/a b/deploy-key": { status: 201, body: out },
    });

    await createRepository({
      name: "a",
      kind: "ssh",
      url: "git@github.com:acme/a.git",
      ssh_known_hosts: "github.com ssh-ed25519 AAAA",
    });
    expect(await generateDeployKey("a b")).toEqual(out);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/repositories",
      "/api/v1/repositories/a%20b/deploy-key",
    ]);
    expect(server.sent[1]?.body).toEqual({});
  });

  it("reads a registered repository's name, kind, URL and trusted host keys", async () => {
    fakeServer({
      "GET /api/v1/repositories/analytics": {
        status: 200,
        body: {
          name: "analytics",
          kind: "ssh",
          url: "git@github.com:acme/a.git",
          ssh_known_hosts: "github.com ssh-ed25519 AAAA\n",
          deploy_keys: [],
        },
      },
    });

    expect(await getRepository("analytics")).toStrictEqual({
      name: "analytics",
      kind: "ssh",
      url: "git@github.com:acme/a.git",
      ssh_known_hosts: "github.com ssh-ed25519 AAAA\n",
    });
  });

  it("edits and replaces keys with the passkey asked first", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "PATCH /api/v1/repositories/analytics": { status: 200, body: { name: "analytics" } },
      "POST /api/v1/repositories/analytics/deploy-key": { status: 201, body: KEYS },
    });
    setStepUp(() => Promise.resolve());

    await updateRepository("analytics", { default_ref: "release" });
    await expect(replaceDeployKey("analytics", "dev")).resolves.toEqual(KEYS);
    await makeMissingDeployKeys("analytics");

    expect(calls(server)).toEqual([
      "GET /api/v1/sign-in/session",
      "PATCH /api/v1/repositories/analytics",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/repositories/analytics/deploy-key",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/repositories/analytics/deploy-key",
    ]);
    expect(server.sent[3]?.body).toEqual({ environments: ["dev"], replace: true });
    expect(server.sent[5]?.body).toEqual({});
  });

  it("deletes without asking for a touch", async () => {
    const server = fakeServer({ "DELETE /api/v1/repositories/analytics": { status: 204 } });

    await deleteRepository("analytics");

    expect(calls(server)).toEqual(["DELETE /api/v1/repositories/analytics"]);
  });

  it("sends only the changed fields", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "PATCH /api/v1/repositories/analytics": { status: 200, body: { name: "analytics" } },
    });
    setStepUp(() => Promise.resolve());

    await updateRepository("analytics", { default_ref: null });

    expect(server.sent[1]?.body).toStrictEqual({ default_ref: null });
  });

  it("encodes the name in the path", async () => {
    const server = fakeServer({
      "GET /api/v1/repositories": { status: 200, body: [] },
      "GET /api/v1/repositories/a b": { status: 200, body: { name: "a b" } },
      "DELETE /api/v1/repositories/x%y": { status: 204 },
    });

    await expect(listRepositories()).resolves.toEqual([]);
    await expect(getRepositoryPage("a b")).resolves.toEqual({ name: "a b" });
    await deleteRepository("x%y");

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/repositories",
      "/api/v1/repositories/a%20b",
      "/api/v1/repositories/x%25y",
    ]);
  });
});
