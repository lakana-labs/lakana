import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Request, test } from "@playwright/test";

import { ADMIN_SESSION } from "./session";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
// Passkeys need a domain as relying-party id, never an IP address: the same preview server,
// reached by its loopback name.
const ORIGIN = "http://localhost:4173";
const KIM = { ...ADMIN_SESSION, name: "kim" };
const KIM_WHOAMI = {
  name: "kim",
  type: "USER",
  task_token: null,
  ci_token: null,
  language: null,
  admin: false,
  has_access: false,
};

/**
 * A random WebAuthn challenge as base64url.
 *
 * @returns The challenge.
 */
function challenge(): string {
  return Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url");
}

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations' ids.
 */
async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  return results.violations.map((violation) => violation.id);
}

/**
 * The fake server of a person who joins with an invite: the session route answers what the
 * last route left, and the browser's requests to the API are recorded.
 *
 * @param page - The page.
 * @returns The requests to the API.
 */
async function fakeServer(page: Page): Promise<{ sent: Request[] }> {
  let session: unknown = null;
  const sent: Request[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) {
      sent.push(request);
    }
  });
  await page.route("**/api/v1/sign-in/session", (route) =>
    session === null
      ? route.fulfill({
          status: 401,
          json: { detail: "Sign in.", type: "AuthenticationError" },
        })
      : route.fulfill({ json: session }),
  );
  await page.route("**/api/v1/sign-in/notices", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/whoami", (route) => route.fulfill({ json: KIM_WHOAMI }));
  await page.route("**/api/v1/sign-in/links/describe", (route) =>
    route.fulfill({ json: { kind: "invite", name: "kim", display_name: null, state: "open" } }),
  );
  await page.route("**/api/v1/sign-in/links/redeem", (route) => {
    session = { ...KIM, via: "invite", needs_passkey: true };
    return route.fulfill({
      json: { name: "kim", needs_passkey: true, offer_passkey: false, csrf_token: "c" },
    });
  });
  await page.route("**/api/v1/principals/kim/passkeys/options", (route) =>
    route.fulfill({
      json: {
        ceremony_id: "reg-1",
        public_key: {
          rp: { id: "localhost", name: "Lakana" },
          user: { id: "a2ltLWhhbmRsZQ", name: "kim", displayName: "kim" },
          challenge: challenge(),
          pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          authenticatorSelection: { residentKey: "required", userVerification: "required" },
          attestation: "none",
          timeout: 60000,
        },
      },
    }),
  );
  await page.route("**/api/v1/principals/kim/passkeys", (route) => {
    session = KIM;
    return route.fulfill({ status: 201, json: { id: "p-1" } });
  });
  await page.route("**/api/v1/sign-in/passkey/options", (route) =>
    route.fulfill({
      json: {
        ceremony_id: "in-1",
        public_key: {
          challenge: challenge(),
          rpId: "localhost",
          allowCredentials: [],
          userVerification: "required",
          timeout: 60000,
        },
      },
    }),
  );
  await page.route("**/api/v1/sign-in/passkey", (route) => {
    session = KIM;
    return route.fulfill({
      json: { name: "kim", needs_passkey: false, offer_passkey: false, csrf_token: "d" },
    });
  });
  await page.route("**/api/v1/principals/kim/sessions", (route) => {
    session = null;
    return route.fulfill({ status: 204 });
  });
  return { sent };
}

test.describe("signing in", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("an invite, a passkey, sign out everywhere, and a passkey sign-in", async ({
    page,
    context,
  }) => {
    const cdp = await context.newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: {
        protocol: "ctap2",
        transport: "internal",
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
      },
    });
    const server = await fakeServer(page);
    // A violation of the content security policy shows up here; a 401 of the session route
    // while signed out is the browser's own log line, not an error of the interface.
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
        errors.push(message.text());
      }
    });
    page.on("pageerror", (error) => errors.push(error.message));

    await page.goto(`${ORIGIN}/sign-in#l=pub-1&s=sec-1`);

    await expect(page.getByRole("heading", { level: 1, name: "Join Lakana as kim" })).toBeFocused();
    expect(page.url()).toBe(`${ORIGIN}/sign-in`);
    expect(await violations(page)).toEqual([]);
    expect(server.sent.map((request) => request.url()).join(" ")).not.toContain("sec-1");

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Not now" })).toHaveCount(0);
    expect(await violations(page), "the passkey step").toEqual([]);
    const redeem = server.sent.find((request) => request.url().endsWith("/redeem"));
    expect(redeem?.postDataJSON()).toEqual({ link_id: "pub-1", secret: "sec-1" });

    await page.getByRole("button", { name: "Add a passkey" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    const registered = server.sent.find((request) =>
      request.url().endsWith("/principals/kim/passkeys"),
    );
    const answer = registered?.postDataJSON() as { ceremony_id: string; credential: unknown };
    expect(answer.ceremony_id).toBe("reg-1");
    expect(answer.credential).toMatchObject({ type: "public-key", response: {} });
    const { credentials } = await cdp.send("WebAuthn.getCredentials", { authenticatorId });
    expect(credentials).toHaveLength(1);
    expect(credentials[0]?.isResidentCredential).toBe(true);
    await expect(page.getByRole("link", { name: "Access" })).toHaveCount(0);
    await expect(page.getByText("You have no access yet; ask an administrator.")).toBeVisible();

    await page.getByRole("button", { name: "Signed in as kim" }).click();
    await page.getByRole("menuitem", { name: "Sign out everywhere" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toBeVisible();
    await expect(page.getByText("You signed out on every device.")).toBeVisible();
    const ended = server.sent.find((request) => request.url().endsWith("/kim/sessions"));
    // The token the link's sign-in answered, kept in the origin's storage; cleared at sign-out.
    expect(ended?.headers()["x-lakana-csrf"]).toBe("c");
    expect(await page.evaluate(() => localStorage.getItem("lakana.csrf"))).toBeNull();
    expect(await violations(page), "the sign-in page").toEqual([]);

    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    const signIn = server.sent.find((request) => request.url().endsWith("/sign-in/passkey"));
    expect(signIn?.postDataJSON()).toMatchObject({
      ceremony_id: "in-1",
      credential: { type: "public-key" },
    });
    expect(errors).toEqual([]);
  });
});
