import { generateKeyPairSync } from "node:crypto";

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Request, test } from "@playwright/test";

import { signInAsAdmin } from "./session";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
// Passkeys need a domain as relying-party id, never an IP address: the same preview server,
// reached by its loopback name.
const ORIGIN = "http://localhost:4173";
const LINK = `${ORIGIN}/sign-in#l=pub-9&s=secret-9`;

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
 * @returns The violations' ids and targets.
 */
async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
}

/**
 * Answers the administration routes like the server: the first invite asks for a passkey touch,
 * and the passkeys list grows when one is added.
 *
 * @param page - The page.
 * @returns The requests sent to the API.
 */
async function fakeAdministration(page: Page): Promise<{ sent: Request[] }> {
  const sent: Request[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) {
      sent.push(request);
    }
  });
  let touched = false;
  let invites: unknown[] = [];
  let passkeys: unknown[] = [];
  await signInAsAdmin(page);
  await page.route("**/api/v1/invites", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: invites });
    }
    if (!touched) {
      return route.fulfill({
        status: 403,
        json: { type: "StepUpRequired", detail: "Confirm with your passkey." },
      });
    }
    invites = [
      {
        id: "i-1",
        name: "sam",
        display_name: "Sam Kay",
        created_at: "2026-11-09T10:00:00Z",
        expires_at: "2026-11-16T10:00:00Z",
      },
    ];
    return route.fulfill({
      status: 201,
      json: {
        id: "i-1",
        name: "sam",
        display_name: "Sam Kay",
        url: LINK,
        expires_at: "2026-11-16T10:00:00Z",
      },
    });
  });
  await page.route("**/api/v1/sign-in/step-up/options", (route) =>
    route.fulfill({
      json: {
        ceremony_id: "up-1",
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
  await page.route("**/api/v1/sign-in/step-up", (route) => {
    touched = true;
    return route.fulfill({ status: 204 });
  });
  await page.route("**/api/v1/principals/admin/passkeys/options", (route) =>
    route.fulfill({
      json: {
        ceremony_id: "reg-1",
        public_key: {
          rp: { id: "localhost", name: "Lakana" },
          user: { id: "YWRtaW4taGFuZGxl", name: "admin", displayName: "admin" },
          challenge: challenge(),
          pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          authenticatorSelection: { residentKey: "required", userVerification: "required" },
          attestation: "none",
          timeout: 60000,
        },
      },
    }),
  );
  await page.route("**/api/v1/principals/admin/passkeys", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: passkeys });
    }
    passkeys = [
      {
        id: "p-2",
        name: "",
        created_at: "2026-11-09T10:00:00Z",
        last_used_at: null,
        backup_eligible: false,
        disabled_reason: null,
        usable_here: true,
      },
    ];
    return route.fulfill({ status: 201, json: passkeys[0] });
  });
  await page.route("**/api/v1/principals/admin/permissions", (route) =>
    route.fulfill({ json: { permissions: [{ via: [{ kind: "grant", role: "ADMIN" }] }] } }),
  );
  await page.route("**/api/v1/principals/admin/sessions", (route) =>
    route.fulfill({
      json: [
        {
          id: "s-1",
          via: "passkey",
          created_at: "2026-11-09T09:00:00Z",
          last_seen_at: "2026-11-09T10:00:00Z",
          absolute_expires_at: "2026-11-16T09:00:00Z",
          user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/155.0.0.0 Safari/537.36",
          browser: "Chrome 155 on Linux",
          peer: "127.0.0.1",
          current: true,
        },
      ],
    }),
  );
  return { sent };
}

test.describe("administering sign-in", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("an invite with a passkey touch, one's passkeys and sessions, and the session lengths", async ({
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
    // The administrator's passkey, already on this device: the step-up touch uses it.
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    await cdp.send("WebAuthn.addCredential", {
      authenticatorId,
      credential: {
        credentialId: Buffer.from("admin-passkey").toString("base64"),
        isResidentCredential: true,
        rpId: "localhost",
        userHandle: Buffer.from("admin-handle").toString("base64"),
        privateKey: privateKey.export({ format: "der", type: "pkcs8" }).toString("base64"),
        signCount: 0,
      },
    });
    const server = await fakeAdministration(page);
    const errors: string[] = [];
    // The step-up refusal (403) is the browser's own log line, not an error of the interface.
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
        errors.push(message.text());
      }
    });
    page.on("pageerror", (error) => errors.push(error.message));

    await page.goto(`${ORIGIN}/access/invites`);
    await expect(page.getByText(/No invite is open/)).toBeVisible();
    expect(await violations(page), "invites").toEqual([]);

    await page.getByRole("button", { name: "Invite someone" }).click();
    const dialog = page.getByRole("dialog", { name: "Invite someone" });
    await expect(dialog).toBeVisible();
    expect(await violations(page), "the invite dialog").toEqual([]);
    await dialog.getByRole("textbox", { name: "Name", exact: true }).fill("sam");
    await dialog.getByRole("textbox", { name: /Display name/ }).fill("Sam Kay");
    await dialog.getByRole("button", { name: "Create the invite" }).click();

    const shown = page.getByRole("dialog", { name: "Send this link to sam" });
    await expect(shown.getByText(LINK)).toBeVisible();
    expect(await violations(page), "the link shown once").toEqual([]);
    const posts = server.sent
      .filter((request) => request.method() === "POST")
      .map((request) => new URL(request.url()).pathname);
    expect(posts).toEqual([
      "/api/v1/invites",
      "/api/v1/sign-in/step-up/options",
      "/api/v1/sign-in/step-up",
      "/api/v1/invites",
    ]);
    const { credentials } = await cdp.send("WebAuthn.getCredentials", { authenticatorId });
    expect(credentials[0]?.signCount).toBe(1);
    await shown.getByRole("button", { name: "Done" }).click();
    await expect(page.getByText(LINK)).toHaveCount(0);
    await expect(page.getByText("Sam Kay")).toBeVisible();

    await page.getByRole("button", { name: "Signed in as admin" }).click();
    await page.getByRole("menuitem", { name: "Passkeys and sessions" }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Passkeys and sessions" }),
    ).toBeVisible();
    await expect(page.getByText("This browser")).toBeVisible();
    await expect(page.getByText("lakana recovery create admin")).toBeVisible();
    expect(await violations(page), "passkeys and sessions").toEqual([]);
    await page.getByRole("button", { name: "Add a passkey" }).click();
    await expect(page.getByRole("button", { name: "Remove Passkey" })).toBeVisible();
    const after = await cdp.send("WebAuthn.getCredentials", { authenticatorId });
    // The device keeps one passkey per account: the new one replaces the earlier one.
    expect(after.credentials.map((credential) => credential.credentialId)).not.toContain(
      Buffer.from("admin-passkey").toString("base64"),
    );

    const remove = page.getByRole("button", { name: "Remove Passkey" });
    await remove.click();
    await expect(page.getByRole("dialog", { name: "Remove Passkey?" })).toBeVisible();
    expect(await violations(page), "the confirmation").toEqual([]);
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(remove).toBeFocused();
    expect(server.sent.some((request) => request.method() === "DELETE")).toBe(false);

    await page.goto(`${ORIGIN}/settings/sign-in`);
    const idle = page.getByRole("textbox", { name: "Idle timeout in minutes" });
    await expect(idle).toHaveValue("720");
    expect(await violations(page), "settings").toEqual([]);
    await idle.fill("5");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("alert")).toContainText("between 15 and 1440 minutes");
    expect(await violations(page), "settings with a refusal").toEqual([]);
    expect(errors).toEqual([]);
  });
});

test("a person's page has a breadcrumb that fits 320 px and passes axe", async ({ page }) => {
  await signInAsAdmin(page);
  const name = "a-person-with-a-rather-long-principal-name";
  await page.route(`**/api/v1/principals/${name}/permissions`, (route) =>
    route.fulfill({ json: { permissions: [] } }),
  );
  await page.route(`**/api/v1/principals/${name}/passkeys`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/v1/principals/${name}/sessions`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/v1/principals/${name}`, (route) =>
    route.fulfill({ json: { name, type: "USER" } }),
  );
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(`/access/people/${name}`);

  const crumbs = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(crumbs.getByText(name)).toHaveAttribute("aria-current", "page");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
  expect(await violations(page), "a person's page at 320 px").toEqual([]);

  await crumbs.getByRole("link", { name: "People" }).click();
  await expect(page).toHaveURL(/\/access$/);
});
