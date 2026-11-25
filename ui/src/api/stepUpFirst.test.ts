import { describe, expect, it, vi } from "vitest";

import { send, setStepUp } from "./client";
import { PROVIDER_TRIP_KEY, ProviderRedirect } from "./oidc";
import { confirmPresence, guarded, STEP_UP_MARGIN_MS } from "./stepUpFirst";

import { fakeServer } from "../test/server";

describe("guarded", () => {
  const STEP_UP = { status: 403, body: { detail: "Touch your passkey.", type: "StepUpRequired" } };

  /**
   * A session whose passkey touch lasts until `inMs` from now; null for no touch.
   *
   * @param inMs - Milliseconds until the touch ends, or null.
   * @returns The route's answer.
   */
  const session = (inMs: number | null) => ({
    status: 200,
    body: {
      step_up_until: inMs === null ? null : new Date(Date.now() + inMs).toISOString(),
    },
  });

  /**
   * A ceremony that sends the step-up requests, as the real one does.
   *
   * @returns The ceremony.
   */
  const ceremony = () =>
    vi.fn(async () => {
      await send("POST", "/api/v1/sign-in/step-up/options", {});
      await send("POST", "/api/v1/sign-in/step-up", {});
    });

  const STEP_UP_ROUTES = {
    "POST /api/v1/sign-in/step-up/options": { status: 200, body: {} },
    "POST /api/v1/sign-in/step-up": { status: 200, body: {} },
  };

  it("asks for the passkey touch before an action that needs one", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": session(null),
      ...STEP_UP_ROUTES,
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    setStepUp(ceremony());

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/grants/g-1",
    ]);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("sends at once while the touch is fresh", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": session(10 * 60_000),
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    const touch = ceremony();
    setStepUp(touch);

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(touch).not.toHaveBeenCalled();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/grants/g-1",
    ]);
  });

  it("asks again a minute before the touch ends", async () => {
    expect(STEP_UP_MARGIN_MS).toBe(60_000);
    fakeServer({
      "GET /api/v1/sign-in/session": session(30_000),
      ...STEP_UP_ROUTES,
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    const touch = ceremony();
    setStepUp(touch);

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(touch).toHaveBeenCalledTimes(1);
  });

  it("a cancelled touch sends nothing", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": session(null),
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    const refused = new DOMException("The touch was cancelled.", "NotAllowedError");
    setStepUp(() => Promise.reject(refused));

    await expect(guarded("DELETE", "/api/v1/grants/g-1")).rejects.toBe(refused);
    expect(server.sent.map((request) => request.method)).toEqual(["GET"]);
  });

  it("still retries once after a StepUpRequired", async () => {
    let calls = 0;
    const server = fakeServer({
      "GET /api/v1/sign-in/session": session(10 * 60_000),
      ...STEP_UP_ROUTES,
      "DELETE /api/v1/grants/g-1": () => {
        calls += 1;
        return calls === 1 ? STEP_UP : { status: 204 };
      },
    });
    const touch = ceremony();
    setStepUp(touch);

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(touch).toHaveBeenCalledTimes(1);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/grants/g-1",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/grants/g-1",
    ]);
  });

  it("guarded sends a linked person through the provider and sends nothing", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { step_up_until: null, provider_linked: true },
      },
      "POST /api/v1/sign-in/oidc/step-up/start": {
        status: 200,
        body: { authorization_url: "https://id.example.org/authorize?state=s" },
      },
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    const touch = ceremony();
    setStepUp(touch);
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, pathname: "/settings/access/grants", search: "?who=kim" });

    await expect(guarded("DELETE", "/api/v1/grants/g-1")).rejects.toBeInstanceOf(ProviderRedirect);

    expect(touch).not.toHaveBeenCalled();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/oidc/step-up/start",
    ]);
    expect(server.sent[1]?.body).toEqual({ return_to: "/settings/access/grants?who=kim" });
    expect(assign).toHaveBeenCalledWith("https://id.example.org/authorize?state=s");
    expect(JSON.parse(sessionStorage.getItem(PROVIDER_TRIP_KEY) ?? "null")).toEqual({
      purpose: "step_up",
      back: "/settings/access/grants?who=kim",
    });
    sessionStorage.clear();
  });

  it("guarded keeps the passkey ceremony for an unlinked person", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { step_up_until: null, provider_linked: false },
      },
      ...STEP_UP_ROUTES,
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    setStepUp(ceremony());

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(server.sent.map((request) => request.path)).not.toContain(
      "/api/v1/sign-in/oidc/step-up/start",
    );
    expect(server.sent.at(-1)?.method).toBe("DELETE");
  });

  it("a linked person's fresh confirmation sends at once", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: {
          step_up_until: new Date(Date.now() + 10 * 60_000).toISOString(),
          provider_linked: true,
        },
      },
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });

    await guarded("DELETE", "/api/v1/grants/g-1");

    expect(server.sent.map((request) => request.method)).toEqual(["GET", "DELETE"]);
  });

  it("a guarded call whose touch runs out inside the margin goes to the provider and is not sent again", async () => {
    let deletes = 0;
    const server = fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: {
          step_up_until: new Date(Date.now() + 10 * 60_000).toISOString(),
          provider_linked: true,
        },
      },
      "DELETE /api/v1/grants/g-1": () => {
        deletes += 1;
        return STEP_UP;
      },
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: { detail: "Confirm at the provider.", type: "ProviderStepUpRequired" },
      },
      "POST /api/v1/sign-in/oidc/step-up/start": {
        status: 200,
        body: { authorization_url: "https://id.example.org/authorize?state=t" },
      },
    });
    setStepUp(confirmPresence);
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, pathname: "/settings/access/grants", search: "" });

    await expect(guarded("DELETE", "/api/v1/grants/g-1")).rejects.toBeInstanceOf(ProviderRedirect);

    expect(deletes).toBe(1);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/grants/g-1",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/oidc/step-up/start",
    ]);
    expect(assign).toHaveBeenCalledWith("https://id.example.org/authorize?state=t");
    sessionStorage.clear();
  });

  it("the ceremony passes any other refusal of the passkey's options on", async () => {
    fakeServer({
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: { detail: "No passkey.", type: "NoPasskeyError" },
      },
    });

    await expect(confirmPresence()).rejects.toMatchObject({ type: "NoPasskeyError" });
  });
});
