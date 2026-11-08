import { describe, expect, it, vi } from "vitest";

import { send } from "./client";
import { addPasskey, signInWithPasskey, stepUp } from "./passkeys";

import { fakeServer } from "../test/server";
import { CREDENTIAL_JSON, fakeWebAuthn } from "../test/webauthn";

const OPTIONS = { ceremony_id: "c-1", public_key: { challenge: "abc" } };

describe("passkeys", () => {
  it("signs in with the browser's own WebAuthn and keeps the CSRF token", async () => {
    const webauthn = fakeWebAuthn();
    const server = fakeServer({
      "POST /api/v1/sign-in/passkey/options": { status: 200, body: OPTIONS },
      "POST /api/v1/sign-in/passkey": {
        status: 200,
        body: { name: "kim", needs_passkey: false, offer_passkey: false, csrf_token: "csrf-5" },
      },
      "POST /api/v1/things": { status: 200, body: {} },
    });

    const signedIn = await signInWithPasskey();
    await send("POST", "/api/v1/things", {});

    expect(signedIn.name).toBe("kim");
    expect(webauthn.parseRequest).toHaveBeenCalledWith({ challenge: "abc" });
    expect(webauthn.get).toHaveBeenCalledWith({
      publicKey: { parsed: "request", options: { challenge: "abc" } },
    });
    expect(server.sent[1]?.body).toEqual({ ceremony_id: "c-1", credential: CREDENTIAL_JSON });
    expect(server.sent[2]?.headers["x-lakana-csrf"]).toBe("csrf-5");
  });

  it("adds a passkey to the person's own account", async () => {
    const webauthn = fakeWebAuthn();
    const server = fakeServer({
      "POST /api/v1/principals/kim/passkeys/options": { status: 200, body: OPTIONS },
      "POST /api/v1/principals/kim/passkeys": { status: 201, body: { id: "p-1" } },
    });

    await addPasskey("kim");

    expect(webauthn.parseCreation).toHaveBeenCalledWith({ challenge: "abc" });
    expect(webauthn.create).toHaveBeenCalledWith({
      publicKey: { parsed: "creation", options: { challenge: "abc" } },
    });
    expect(server.sent[1]?.body).toEqual({ ceremony_id: "c-1", credential: CREDENTIAL_JSON });
  });

  it("confirms the session with a passkey touch (step-up)", async () => {
    fakeWebAuthn();
    const server = fakeServer({
      "POST /api/v1/sign-in/step-up/options": { status: 200, body: OPTIONS },
      "POST /api/v1/sign-in/step-up": {
        status: 200,
        body: { step_up_until: "2026-11-08T10:15:00Z" },
      },
    });

    await stepUp();

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/sign-in/step-up/options",
      "/api/v1/sign-in/step-up",
    ]);
    expect(server.sent[1]?.body).toEqual({ ceremony_id: "c-1", credential: CREDENTIAL_JSON });
  });

  it("passes the browser's refusal on and sends no answer", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValueOnce(new DOMException("cancelled", "NotAllowedError"));
    const server = fakeServer({
      "POST /api/v1/sign-in/passkey/options": { status: 200, body: OPTIONS },
    });

    await expect(signInWithPasskey()).rejects.toMatchObject({ name: "NotAllowedError" });
    expect(server.sent).toHaveLength(1);
  });

  it("treats a browser that hands back no credential as cancelled", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.create.mockResolvedValueOnce(null);
    fakeServer({ "POST /api/v1/principals/kim/passkeys/options": { status: 200, body: OPTIONS } });

    await expect(addPasskey("kim")).rejects.toMatchObject({ name: "NotAllowedError" });
  });

  it("names a browser without passkeys before asking the server", async () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    const server = fakeServer();

    await expect(signInWithPasskey()).rejects.toMatchObject({ name: "NotSupportedError" });
    expect(server.sent).toHaveLength(0);
  });
});
