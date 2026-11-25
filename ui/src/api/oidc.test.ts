import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getProvider,
  goToProvider,
  PROVIDER_TRIP_KEY,
  ProviderRedirect,
  startProviderSignIn,
  startWithLink,
} from "./oidc";

import { fakeServer } from "../test/server";

const STARTED = { status: 200, body: { authorization_url: "https://id.example.org/authorize" } };

describe("the provider's first calls", () => {
  afterEach(() => {
    sessionStorage.clear();
  });

  it("reads whether the provider is on, in the background", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/oidc": { status: 200, body: { enabled: true, display_name: "Okta" } },
    });

    expect(await getProvider()).toEqual({ enabled: true, display_name: "Okta" });
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("starting keeps where the person was going on the server", async () => {
    const server = fakeServer({ "POST /api/v1/sign-in/oidc/start": STARTED });

    expect(await startProviderSignIn("/sign-in/cli?code=c")).toBe(
      "https://id.example.org/authorize",
    );
    expect(server.sent[0]?.body).toEqual({ return_to: "/sign-in/cli?code=c" });
  });

  it("starts with a link's two halves in the body only", async () => {
    const server = fakeServer({ "POST /api/v1/sign-in/oidc/start": STARTED });

    await startWithLink("pub", "sec", "invite");

    expect(server.sent[0]?.path).toBe("/api/v1/sign-in/oidc/start");
    expect(server.sent[0]?.body).toEqual({ purpose: "invite", link_id: "pub", secret: "sec" });
  });

  it("goes to the provider and remembers an in-session flow", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });

    const error = goToProvider("https://id.example.org/a", { purpose: "link", back: "/account" });

    expect(error).toBeInstanceOf(ProviderRedirect);
    expect(error.name).toBe("ProviderRedirect");
    expect(assign).toHaveBeenCalledWith("https://id.example.org/a");
    expect(sessionStorage.getItem(PROVIDER_TRIP_KEY)).toBe(
      JSON.stringify({ purpose: "link", back: "/account" }),
    );
  });

  it("a sign-in remembers nothing, and blocked storage does not stop the trip", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });
    goToProvider("https://id.example.org/a");
    expect(sessionStorage.getItem(PROVIDER_TRIP_KEY)).toBeNull();

    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    goToProvider("https://id.example.org/b", { purpose: "step_up", back: "/" });
    setItem.mockRestore();
    expect(assign).toHaveBeenLastCalledWith("https://id.example.org/b");
  });

  it.each(["javascript:alert(1)", "data:text/html,x", "/relative", "not a url"])(
    "refuses to follow %s",
    (url) => {
      const assign = vi.fn();
      vi.stubGlobal("location", { assign });

      expect(() => goToProvider(url, { purpose: "link", back: "/account" })).toThrow(
        /not a web address/,
      );
      expect(assign).not.toHaveBeenCalled();
      expect(sessionStorage.getItem(PROVIDER_TRIP_KEY)).toBeNull();
    },
  );

  it("follows an http address", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });
    goToProvider("http://keycloak.internal:8080/realms/corp/auth");
    expect(assign).toHaveBeenCalledWith("http://keycloak.internal:8080/realms/corp/auth");
  });
});
