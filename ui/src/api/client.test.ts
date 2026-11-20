import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  CSRF_STORAGE_KEY,
  DownloadCut,
  DownloadTooLong,
  WrongFileType,
  api,
  endSession,
  onSessionEnd,
  onUnauthorized,
  send,
  setCsrfToken,
  setStepUp,
  stepUpNow,
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

  it("keeps a request alive past the page's end only when asked", async () => {
    const server = fakeServer({ "POST /api/v1/things/1/cancel": { status: 204 } });

    await send("POST", "/api/v1/things/1/cancel", undefined, { background: true, keepalive: true });
    await send("POST", "/api/v1/things/1/cancel");

    expect(server.sent[0]?.keepalive).toBe(true);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
    expect(server.sent[1]?.keepalive).toBeUndefined();
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

  it("ends the session on a 401 and on endSession, forgetting the token, until they stop listening", async () => {
    fakeServer({ "GET /api/v1/things": { status: 401, body: { detail: "x", type: "y" } } });
    const listener = vi.fn();
    const stop = onSessionEnd(listener);

    await send("GET", "/api/v1/things").catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(1);
    setCsrfToken("csrf-1");
    endSession();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
    stop();
    endSession();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("tells every listener even when one throws, and keeps the refusal it ends with", async () => {
    fakeServer({ "GET /api/v1/things": { status: 401, body: { detail: "Sign in.", type: "y" } } });
    const reported = vi.spyOn(window, "reportError").mockImplementation(() => undefined);
    const broken = new Error("listener broke");
    const throwing = (): void => {
      throw broken;
    };
    const after = vi.fn();
    const afterUnauthorized = vi.fn();
    const stops = [
      onUnauthorized(throwing),
      onUnauthorized(afterUnauthorized),
      onSessionEnd(throwing),
      onSessionEnd(after),
    ];

    const refused = await send("GET", "/api/v1/things").catch((error: unknown) => error);
    expect(refused).toBeInstanceOf(ApiError);
    expect((refused as ApiError).detail).toBe("Sign in.");
    expect(afterUnauthorized).toHaveBeenCalledTimes(1);
    expect(after).toHaveBeenCalledTimes(1);

    expect(() => {
      endSession();
    }).not.toThrow();
    expect(after).toHaveBeenCalledTimes(2);
    expect(reported).toHaveBeenCalledTimes(3);
    expect(reported).toHaveBeenCalledWith(broken);
    for (const stop of stops) {
      stop();
    }
    reported.mockRestore();
  });
});

describe("api.upload and api.blob", () => {
  it("uploads a file as the body, with its type and the CSRF header", async () => {
    const server = fakeServer({
      "PUT /api/v1/principals/admin/picture": { status: 200, body: { etag: "e1" } },
    });
    setCsrfToken("csrf-1");
    const file = new Blob(["png"], { type: "image/png" });

    await expect(
      api.upload("PUT", "/api/v1/principals/admin/picture", file, "image/png"),
    ).resolves.toEqual({ etag: "e1" });

    expect(server.sent[0]?.headers).toMatchObject({
      "content-type": "image/png",
      "x-lakana-csrf": "csrf-1",
    });
    expect(server.sent[0]?.raw).toBe(file);
  });

  it("reads a file with the CSRF header, as a background request", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/admin/picture": {
        status: 200,
        blob: new Blob(["webp"], { type: "image/webp" }),
      },
    });
    setCsrfToken("csrf-1");

    const read = await api.blob("/api/v1/principals/admin/picture");

    expect(read.size).toBe(4);
    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("csrf-1");
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("download sends the CSRF token and asks for octet-stream, not as a background request", async () => {
    const server = fakeServer({
      "GET /api/v1/files/one": { status: 200, blob: new Blob(["bytes"]) },
    });
    setCsrfToken("csrf-1");

    const read = await api.download("/api/v1/files/one", 5);

    expect(await read.text()).toBe("bytes");
    expect(server.sent[0]?.headers).toMatchObject({
      accept: "application/octet-stream",
      "x-lakana-csrf": "csrf-1",
    });
    expect(server.sent[0]?.headers["x-lakana-background"]).toBeUndefined();
  });

  it("download keeps octet-stream for artifacts", async () => {
    const server = fakeServer({
      "GET /api/v1/files/one": { status: 200, blob: new Blob(["bytes"]) },
    });

    await api.download("/api/v1/files/one", 5);

    expect(server.sent[0]?.headers["accept"]).toBe("application/octet-stream");
  });

  it("download asks for the type it is given and refuses another", async () => {
    const server = fakeServer({
      "GET /api/v1/files/csv": { status: 200, blob: new Blob(["a,b"], { type: "text/csv" }) },
      "GET /api/v1/files/page": { status: 200, blob: new Blob(["<p>"], { type: "text/html" }) },
    });

    const csv = await api.download("/api/v1/files/csv", 5, "text/csv");
    await expect(api.download("/api/v1/files/page", 5, "text/csv")).rejects.toBeInstanceOf(
      WrongFileType,
    );
    await expect(api.download("/api/v1/files/page", 5)).rejects.toBeInstanceOf(WrongFileType);

    expect(await csv.text()).toBe("a,b");
    expect(server.sent[0]?.headers["accept"]).toBe("text/csv");
  });

  it("download refuses an answer that names no type", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            new ReadableStream({
              start(c) {
                c.close();
              },
            }),
            { status: 200 },
          ),
        ),
      ),
    );

    await expect(api.download("/api/v1/files/one", 5)).rejects.toBeInstanceOf(WrongFileType);
  });

  it("download refuses an answer without a body that names no type, such as a 204", async () => {
    fakeServer({ "GET /api/v1/files/one": { status: 204 } });

    await expect(api.download("/api/v1/files/one", 5)).rejects.toBeInstanceOf(WrongFileType);
  });

  it("download reads an answer of the asked type without a body as an empty file", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(null, {
            status: 200,
            headers: { "Content-Type": "application/octet-stream" },
          }),
        ),
      ),
    );

    const read = await api.download("/api/v1/files/one", 5);

    expect(read.size).toBe(0);
  });

  /**
   * Answers every request with a body the test streams.
   *
   * @param source - The body's source.
   */
  function streamed(source: UnderlyingDefaultSource<Uint8Array>): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(new ReadableStream(source), {
            status: 200,
            headers: { "Content-Type": "application/octet-stream" },
          }),
        ),
      ),
    );
  }

  it("download stops reading once the body passes the limit and cancels the rest", async () => {
    // An endless body: a server or a proxy that sends more than the manifest records.
    let pulls = 0;
    let cancelled = false;
    streamed({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(4));
      },
      cancel() {
        cancelled = true;
      },
    });

    await expect(api.download("/api/v1/files/one", 10)).rejects.toBeInstanceOf(DownloadTooLong);

    expect(cancelled).toBe(true);
    expect(pulls).toBeLessThan(10);
  });

  it("download names a body cut in the middle, and passes any other error on", async () => {
    streamed({
      start(controller) {
        controller.enqueue(new Uint8Array(2));
        controller.error(new TypeError("network error"));
      },
    });
    await expect(api.download("/api/v1/files/one", 10)).rejects.toBeInstanceOf(DownloadCut);

    streamed({
      start(controller) {
        controller.error(new RangeError("other"));
      },
    });
    await expect(api.download("/api/v1/files/one", 10)).rejects.toBeInstanceOf(RangeError);
  });

  it("asks the server again before showing a file the browser has cached", async () => {
    // A replaced picture keeps its URL; only the etag changes, so a cached copy must be checked.
    const server = fakeServer({
      "GET /api/v1/principals/admin/picture": {
        status: 200,
        blob: new Blob(["webp"], { type: "image/webp" }),
      },
    });

    await api.blob("/api/v1/principals/admin/picture");

    expect(server.sent[0]?.cache).toBe("no-cache");
  });

  it("turns a refused upload or read into an ApiError", async () => {
    fakeServer({
      "PUT /api/v1/principals/admin/picture": {
        status: 422,
        body: { detail: "The file is not a readable PNG image.", type: "ValidationError" },
      },
      "GET /api/v1/principals/admin/picture": { status: 404, body: { detail: "No picture." } },
    });

    await expect(
      api.upload("PUT", "/api/v1/principals/admin/picture", new Blob(["x"]), "image/png"),
    ).rejects.toMatchObject({ status: 422, detail: "The file is not a readable PNG image." });
    await expect(api.blob("/api/v1/principals/admin/picture")).rejects.toMatchObject({
      status: 404,
    });
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

describe("stepUpNow", () => {
  it("does nothing before a ceremony is set", async () => {
    vi.resetModules();
    const fresh = await import("./client");
    const server = fakeServer({});

    await fresh.stepUpNow();

    expect(server.sent).toEqual([]);
  });

  it("runs the ceremony", async () => {
    const ceremony = vi.fn(() => Promise.resolve());
    setStepUp(ceremony);

    await stepUpNow();

    expect(ceremony).toHaveBeenCalledTimes(1);
  });
});
