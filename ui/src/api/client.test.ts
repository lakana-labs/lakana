import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  CSRF_STORAGE_KEY,
  api,
  onUnauthorized,
  send,
  setCsrfToken,
  setStepUp,
} from "./client";

import { fakeServer } from "../test/server";

describe("send", () => {
  it("sends the CSRF token on every request, reads too", async () => {
    const server = fakeServer({
      "GET /api/v1/things": { status: 200, body: [] },
      "POST /api/v1/things": { status: 201, body: {} },
      "PATCH /api/v1/things": { status: 200, body: {} },
      "DELETE /api/v1/things": { status: 204 },
    });
    setCsrfToken("csrf-1");

    await send("GET", "/api/v1/things");
    await send("POST", "/api/v1/things", { a: 1 });
    await send("PATCH", "/api/v1/things", { a: 2 });
    await send("DELETE", "/api/v1/things");

    expect(server.sent.map((request) => request.headers["x-lakana-csrf"])).toEqual([
      "csrf-1",
      "csrf-1",
      "csrf-1",
      "csrf-1",
    ]);
  });

  it("keeps the token in the origin's storage, so a new tab of the same origin has it", async () => {
    const server = fakeServer({ "GET /api/v1/things": { status: 200, body: [] } });
    setCsrfToken("csrf-1");
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-1");
    localStorage.setItem(CSRF_STORAGE_KEY, "from-another-tab");

    await send("GET", "/api/v1/things");

    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("from-another-tab");
    setCsrfToken(null);
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
  });

  it("forgets the token on a 401", async () => {
    const server = fakeServer({
      "GET /api/v1/things": {
        status: 401,
        body: { detail: "Sign in again.", type: "CsrfRefused" },
      },
      "POST /api/v1/things": { status: 200, body: {} },
    });
    setCsrfToken("csrf-1");

    await expect(send("GET", "/api/v1/things")).rejects.toBeInstanceOf(ApiError);
    await send("POST", "/api/v1/things", {});

    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
    expect(server.sent[1]?.headers).not.toHaveProperty("x-lakana-csrf");
  });

  it("keeps a newer token another tab stored while a request failed", async () => {
    const server = fakeServer({
      "GET /api/v1/things": () => {
        localStorage.setItem(CSRF_STORAGE_KEY, "newer");
        return { status: 401, body: { detail: "x", type: "SessionEndedError" } };
      },
      "POST /api/v1/things": { status: 200, body: {} },
    });
    setCsrfToken("older");

    await send("GET", "/api/v1/things").catch(() => undefined);
    await send("POST", "/api/v1/things", {});

    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("newer");
    expect(server.sent[1]?.headers["x-lakana-csrf"]).toBe("newer");
  });

  it("keeps the token in memory when the browser blocks storage", async () => {
    const server = fakeServer({ "GET /api/v1/things": { status: 200, body: [] } });
    const blocked = (): never => {
      throw new DOMException("blocked", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(blocked);
    setCsrfToken("in-memory");

    await send("GET", "/api/v1/things");
    setCsrfToken(null);
    await send("GET", "/api/v1/things");

    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("in-memory");
    expect(server.sent[1]?.headers).not.toHaveProperty("x-lakana-csrf");
    vi.restoreAllMocks();
  });

  it("sends no CSRF header before a session gave one", async () => {
    const server = fakeServer({
      "POST /api/v1/sign-in/passkey/options": { status: 200, body: {} },
    });

    await send("POST", "/api/v1/sign-in/passkey/options", {});

    expect(server.sent[0]?.headers).not.toHaveProperty("x-lakana-csrf");
  });

  it("marks polls as background", async () => {
    const server = fakeServer({ "GET /api/v1/things": { status: 200, body: [] } });

    await send("GET", "/api/v1/things", undefined, { background: true });
    await send("GET", "/api/v1/things");

    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
    expect(server.sent[1]?.headers).not.toHaveProperty("x-lakana-background");
  });

  it("sends JSON with the browser's own cookie, never a token", async () => {
    const fetchStub = vi.fn(() => Promise.resolve(Response.json({ ok: true })));
    vi.stubGlobal("fetch", fetchStub);

    const answer = await send<{ ok: boolean }>("POST", "/api/v1/things", { a: 1 });

    expect(answer).toEqual({ ok: true });
    const [path, init] = fetchStub.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/v1/things");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe('{"a":1}');
    const headers = new Headers(init.headers);
    expect(headers.get("content-type")).toBe("application/json");
    expect(headers.get("accept")).toBe("application/json");
    expect(headers.has("authorization")).toBe(false);
  });

  it("answers undefined for 204", async () => {
    fakeServer({ "DELETE /api/v1/things": { status: 204 } });

    await expect(send("DELETE", "/api/v1/things")).resolves.toBeUndefined();
  });

  it("turns a refusal into an ApiError with the server's cause", async () => {
    fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 410,
        body: {
          detail: "This sign-in link has expired.",
          type: "LinkUnusableError",
          state: "expired",
        },
      },
      "GET /api/v1/sign-in/session": {
        status: 401,
        body: { detail: "Your session ended.", type: "SessionEndedError", reason: "idle" },
      },
    });

    const gone = await send("POST", "/api/v1/sign-in/links/describe", {}).catch(
      (error: unknown) => error,
    );
    const ended = await send("GET", "/api/v1/sign-in/session").catch((error: unknown) => error);

    expect(gone).toBeInstanceOf(ApiError);
    expect(gone).toMatchObject({
      status: 410,
      type: "LinkUnusableError",
      detail: "This sign-in link has expired.",
      state: "expired",
      reason: undefined,
      message: "This sign-in link has expired.",
    });
    expect(ended).toMatchObject({ status: 401, type: "SessionEndedError", reason: "idle" });
  });

  it("joins a validation error's problems into one detail", async () => {
    fakeServer({
      "POST /api/v1/things": {
        status: 422,
        body: { detail: [{ msg: "Field required" }, { msg: "Too long" }, "Odd"] },
      },
    });

    await expect(send("POST", "/api/v1/things", {})).rejects.toMatchObject({
      status: 422,
      type: "ValidationError",
      detail: "Field required; Too long; Odd",
    });
  });

  it("names the status when the answer is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }),
        ),
      ),
    );

    await expect(send("GET", "/api/v1/things")).rejects.toMatchObject({
      status: 502,
      type: "HttpError",
      detail: "The server answered 502 Bad Gateway.",
    });
  });

  it("names an answer that is not JSON as the server's failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("<html>proxy page</html>", { status: 200 }))),
    );

    await expect(send("GET", "/api/v1/things")).rejects.toMatchObject({
      status: 200,
      type: "HttpError",
      detail: "The server answered 200 with something that is not JSON.",
    });
  });

  it("names an unreachable server as a network error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    await expect(send("GET", "/api/v1/things")).rejects.toMatchObject({
      status: 0,
      type: "NetworkError",
    });
  });

  it("tells the listeners about a 401 until they stop listening", async () => {
    fakeServer({ "GET /api/v1/things": { status: 401, body: { detail: "x", type: "y" } } });
    const listener = vi.fn();
    const stop = onUnauthorized(listener);

    await send("GET", "/api/v1/things").catch(() => undefined);
    stop();
    await send("GET", "/api/v1/things").catch(() => undefined);

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0]?.[0]).toBeInstanceOf(ApiError);
  });
});

describe("api.request", () => {
  const STEP_UP = { status: 403, body: { detail: "Touch your passkey.", type: "StepUpRequired" } };

  it("runs the step-up ceremony on StepUpRequired and retries once", async () => {
    let calls = 0;
    const server = fakeServer({
      "POST /api/v1/invites": () => {
        calls += 1;
        return calls === 1 ? STEP_UP : { status: 201, body: { id: "i-1" } };
      },
    });
    const ceremony = vi.fn(() => Promise.resolve());
    setStepUp(ceremony);

    const answer = await api.request<{ id: string }>("POST", "/api/v1/invites", { name: "kim" });

    expect(answer).toEqual({ id: "i-1" });
    expect(ceremony).toHaveBeenCalledTimes(1);
    expect(server.sent.map((request) => request.body)).toEqual([{ name: "kim" }, { name: "kim" }]);
  });

  it("sends the CSRF token on the retry too", async () => {
    let calls = 0;
    const server = fakeServer({
      "POST /api/v1/invites": () => {
        calls += 1;
        return calls === 1 ? STEP_UP : { status: 201, body: {} };
      },
    });
    setCsrfToken("csrf-7");
    setStepUp(() => Promise.resolve());

    await api.request("POST", "/api/v1/invites", {});

    expect(server.sent.map((request) => request.headers["x-lakana-csrf"])).toEqual([
      "csrf-7",
      "csrf-7",
    ]);
  });

  it("asks for one passkey touch when two actions need it at once", async () => {
    const seen = new Map<string, number>();
    const server = fakeServer({
      "POST /api/v1/a": (request) => {
        seen.set(request.path, (seen.get(request.path) ?? 0) + 1);
        return seen.get(request.path) === 1 ? STEP_UP : { status: 200, body: "a" };
      },
      "POST /api/v1/b": (request) => {
        seen.set(request.path, (seen.get(request.path) ?? 0) + 1);
        return seen.get(request.path) === 1 ? STEP_UP : { status: 200, body: "b" };
      },
    });
    let touch: () => void = () => undefined;
    const ceremony = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          touch = resolve;
        }),
    );
    setStepUp(ceremony);

    const both = Promise.all([
      api.request("POST", "/api/v1/a", {}),
      api.request("POST", "/api/v1/b", {}),
    ]);
    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });
    // Both refusals have arrived and both requests wait for the one touch.
    await new Promise((resolve) => setTimeout(resolve, 20));
    touch();

    await expect(both).resolves.toEqual(["a", "b"]);
    expect(ceremony).toHaveBeenCalledTimes(1);

    // The next action that needs a touch asks again.
    seen.clear();
    const third = api.request("POST", "/api/v1/a", {});
    await vi.waitFor(() => {
      expect(ceremony).toHaveBeenCalledTimes(2);
    });
    touch();
    await expect(third).resolves.toBe("a");
  });

  it("retries only once", async () => {
    const server = fakeServer({ "POST /api/v1/invites": STEP_UP });
    const ceremony = vi.fn(() => Promise.resolve());
    setStepUp(ceremony);

    await expect(api.request("POST", "/api/v1/invites", {})).rejects.toMatchObject({
      type: "StepUpRequired",
    });
    expect(ceremony).toHaveBeenCalledTimes(1);
    expect(server.sent).toHaveLength(2);
  });

  it("never asks for a passkey touch on a background request", async () => {
    fakeServer({ "GET /api/v1/things": STEP_UP });
    const ceremony = vi.fn(() => Promise.resolve());
    setStepUp(ceremony);

    await expect(
      api.request("GET", "/api/v1/things", undefined, { background: true }),
    ).rejects.toMatchObject({ type: "StepUpRequired" });
    expect(ceremony).not.toHaveBeenCalled();
  });

  it("passes every other refusal on", async () => {
    fakeServer({
      "GET /api/v1/things": { status: 403, body: { detail: "No.", type: "PermissionDenied" } },
    });
    const ceremony = vi.fn(() => Promise.resolve());
    setStepUp(ceremony);

    await expect(api.request("GET", "/api/v1/things")).rejects.toMatchObject({
      type: "PermissionDenied",
    });
    expect(ceremony).not.toHaveBeenCalled();
  });
});
