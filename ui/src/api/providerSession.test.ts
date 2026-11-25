import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, CSRF_STORAGE_KEY, setStepUp } from "./client";
import { PROVIDER_TRIP_KEY, ProviderRedirect } from "./oidc";
import {
  finishInSession,
  finishProviderSignIn,
  linkToProvider,
  takeProviderTrip,
  tokenWindowEndsAt,
  unlinkProvider,
} from "./providerSession";

import { fakeServer } from "../test/server";

const STALE = { status: 200, body: { step_up_until: null, provider_linked: false } };

/**
 * A ceremony that records that it ran.
 *
 * @returns The ceremony.
 */
const ceremony = () => vi.fn(() => Promise.resolve());

describe("the provider's calls of the pages loaded on demand", () => {
  afterEach(() => {
    sessionStorage.clear();
  });

  it("finish stores the CSRF token and returns the path", async () => {
    fakeServer({
      "POST /api/v1/sign-in/oidc/finish": {
        status: 200,
        body: { name: "kim", csrf_token: "csrf-1", return_to: "/catalog/prod" },
      },
    });

    expect(await finishProviderSignIn()).toEqual({ name: "kim", return_to: "/catalog/prod" });
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-1");
  });

  it("finish passes a refusal on as ApiError with reason", async () => {
    fakeServer({
      "POST /api/v1/sign-in/oidc/finish": {
        status: 403,
        body: { detail: "Not linked.", type: "OidcSignInRefused", reason: "not_linked" },
      },
    });

    const refused = await finishProviderSignIn().catch((error: unknown) => error);

    expect(refused).toBeInstanceOf(ApiError);
    expect(refused).toMatchObject({ status: 403, reason: "not_linked" });
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
  });

  it("finishes in the session", async () => {
    const server = fakeServer({
      "POST /api/v1/sign-in/oidc/finish-in-session": {
        status: 200,
        body: { return_to: "/account", step_up_until: "2026-11-25T10:15:00Z" },
      },
    });

    expect(await finishInSession()).toEqual({
      return_to: "/account",
      step_up_until: "2026-11-25T10:15:00Z",
    });
    expect(server.sent[0]?.method).toBe("POST");
  });

  it("reads and forgets the trip this tab started", () => {
    sessionStorage.setItem(PROVIDER_TRIP_KEY, JSON.stringify({ purpose: "link", back: "/a" }));
    expect(takeProviderTrip()).toEqual({ purpose: "link", back: "/a" });
    expect(takeProviderTrip()).toBeNull();
    sessionStorage.setItem(PROVIDER_TRIP_KEY, JSON.stringify({ purpose: "other", back: "/a" }));
    expect(takeProviderTrip()).toBeNull();
    sessionStorage.setItem(PROVIDER_TRIP_KEY, JSON.stringify({ purpose: "link", back: 3 }));
    expect(takeProviderTrip()).toBeNull();
    sessionStorage.setItem(PROVIDER_TRIP_KEY, "null");
    expect(takeProviderTrip()).toBeNull();
    sessionStorage.setItem(PROVIDER_TRIP_KEY, "{");
    expect(takeProviderTrip()).toBeNull();
  });

  it("links after the touch and leaves for the provider", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": STALE,
      "POST /api/v1/sign-in/oidc/link/start": {
        status: 200,
        body: { authorization_url: "https://id.example.org/link" },
      },
    });
    const touch = ceremony();
    setStepUp(touch);
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, pathname: "/account", search: "" });

    await expect(linkToProvider("/account")).rejects.toBeInstanceOf(ProviderRedirect);

    expect(touch).toHaveBeenCalledTimes(1);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/oidc/link/start",
    ]);
    expect(server.sent[1]?.body).toEqual({ return_to: "/account" });
    expect(assign).toHaveBeenCalledWith("https://id.example.org/link");
    expect(takeProviderTrip()).toEqual({ purpose: "link", back: "/account" });
  });

  it("reads the tokens' window", async () => {
    fakeServer({
      "GET /api/v1/whoami": { status: 200, body: { token_window_ends_at: "2026-12-25T09:00:00Z" } },
    });
    expect(await tokenWindowEndsAt()).toBe("2026-12-25T09:00:00Z");
    fakeServer({ "GET /api/v1/whoami": { status: 200, body: {} } });
    expect(await tokenWindowEndsAt()).toBeNull();
  });

  it("unlinkProvider encodes the name and asks for the touch first", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": STALE,
      "DELETE /api/v1/principals/kim o/oidc-link": { status: 204 },
    });
    const touch = ceremony();
    setStepUp(touch);

    await unlinkProvider("kim o");

    expect(touch).toHaveBeenCalledTimes(1);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/principals/kim%20o/oidc-link",
    ]);
  });
});
