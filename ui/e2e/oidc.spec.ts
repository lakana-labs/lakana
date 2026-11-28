import { expect, type Page, test } from "@playwright/test";

import {
  OIDC_STUBBED,
  PROVIDER_URL,
  type ProviderWorld,
  stubInvite,
  stubLinkedKim,
  stubProvider,
} from "./oidc";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

const REASONS = [
  "not_linked",
  "deactivated",
  "provider_unreachable",
  "provider_refused",
  "token_invalid",
  "flow_expired",
  "identity_taken",
  "identity_mismatch",
  "link_unusable",
  "last_unlinked_admin",
  "cannot_link_operator",
  "already_linked",
  "auth_too_old",
];

/**
 * Says whether the focus is on the page's body, which a keyboard user cannot see.
 *
 * @param page - The page.
 * @returns True when nothing has the focus.
 */
async function focusOnBody(page: Page): Promise<boolean> {
  return page.evaluate(() => document.activeElement === document.body);
}

/**
 * The requests to the API that no stub answers.
 *
 * @param world - The stubbed server's state.
 * @returns The unstubbed `METHOD path`s.
 */
function unstubbed(world: ProviderWorld): string[] {
  const known = new Set([...STUBBED, ...OIDC_STUBBED]);
  return world.sent.filter((sent) => {
    const path = sent.split(" ")[1] ?? "";
    return !path.startsWith("https://id.example.org/") && !known.has(path);
  });
}

/**
 * The console errors but the browser's own line for an answer the flow expects: the 401 of a
 * signed-out browser's session read and the 403 of a refusal.
 *
 * @param errors - The console errors.
 * @returns The others.
 */
function unexpected(errors: readonly string[]): string[] {
  return errors.filter(
    (error) =>
      !/^Failed to load resource: the server responded with a status of 40[13] /.test(error),
  );
}

/**
 * Says whether the page scrolls sideways.
 *
 * @param page - The page.
 * @returns True when it is wider than the window.
 */
async function scrollsSideways(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
}

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript(
        ([key, value]) => {
          window.localStorage.setItem(key ?? "", value ?? "");
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
      await signInAsAdmin(page);
    });

    test("sign in with the provider by keyboard", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { signedIn: false });
      world.returnTo = "/catalog";
      await page.goto("/catalog");

      await expect(
        page.getByRole("heading", { level: 1, name: "Sign in to Lakana" }),
      ).toBeFocused();
      const provider = page.getByRole("button", { name: "Sign in with Okta" });
      const passkey = page.getByRole("button", { name: "Use a passkey" });
      await expect(provider).toBeVisible();
      await expect(passkey).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await tabTo(page, provider);
      expect(await focusOnBody(page)).toBe(false);
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
      expect(new URL(page.url()).pathname).toBe("/catalog");
      expect(world.bodies.get("POST /api/v1/sign-in/oidc/start")).toEqual({
        return_to: "/catalog",
      });
      expect(world.sent).toContain(`GET ${PROVIDER_URL}?flow=start`);
      expect(world.sent).toContain("POST /api/v1/sign-in/oidc/finish");
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("a first sign-in through the provider opens the welcome, and no step speaks of passkeys", async ({
      page,
    }) => {
      const errors = consoleErrors(page);
      // A person who signs in through the provider: the session says so once they are back.
      const world = await stubProvider(page, { signedIn: false, linked: true });
      // A first sign-in: the tour not yet seen (a later route wins over the session's stub).
      await page.route(
        (url) => url.pathname === "/api/v1/principals/admin/interface-state",
        (route) =>
          route.fulfill({
            json: { tour_seen: false, get_started_hidden: true, update_note_seen: null },
          }),
      );
      // Get started as the server answers it for such a person: no "Add a passkey" step.
      await page.route(
        (url) => url.pathname === "/api/v1/home/get-started",
        (route) =>
          route.fulfill({
            json: {
              audience: "admin",
              local: false,
              steps: [
                { id: "connect_repository", done: true },
                { id: "invite_team", done: true },
                { id: "deploy_project", done: false },
              ],
              hidden: true,
              complete: false,
            },
          }),
      );
      world.returnTo = "/";
      await page.goto("/");
      await page.getByRole("button", { name: "Sign in with Okta" }).click();

      const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
      await expect(welcome).toBeVisible();
      expect(world.sent).toContain("POST /api/v1/sign-in/oidc/finish");
      await expect(welcome).not.toContainText(/passkey|provider|Okta/i);
      await welcome.getByRole("button", { name: "Start the tour" }).click();
      const seen: string[] = [];
      for (;;) {
        const step = page.getByRole("dialog", { name: /./ });
        await expect(step).toBeVisible();
        // The tour's own words, and the next step it repeats from Get started, name neither the
        // way in nor the provider.
        const words = await step.locator("h2, p, li").allTextContents();
        expect(words.length).toBeGreaterThan(1);
        expect(words.join(" ")).not.toMatch(/passkey|provider|Okta/i);
        seen.push((await step.getByRole("heading", { level: 2 }).textContent()) ?? "");
        const next = step.getByRole("button", { name: "Next" });
        if ((await next.count()) === 0) break;
        await next.click();
      }
      expect(seen).toEqual([
        "Home",
        "Catalog",
        "Queries",
        "Jobs",
        "Connections",
        "Settings",
        "Search",
        "Help",
      ]);
      // A linked person is not asked for a passkey: the step after it is their next one.
      await expect(page.getByText("Your next step:")).toContainText("Open the tutorial");
      await expect(page.getByText("Your next step:")).not.toContainText(/passkey/i);
      await page.getByRole("button", { name: "Finish" }).click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("every refusal page passes axe, its heading focused", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { signedIn: false });
      for (const reason of REASONS) {
        world.refuse = reason;
        await page.goto("/sign-in/oidc");
        const heading = page.getByRole("heading", { level: 1, name: "You were not signed in" });
        await expect(heading, reason).toBeFocused();
        await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
        expect(await violations(page), reason).toEqual([]);
      }
      await tabTo(page, page.getByRole("button", { name: "Try again" }));
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("heading", { level: 1, name: "Sign in to Lakana" }),
      ).toBeVisible();
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("an invite through the provider", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { signedIn: false });
      await stubInvite(page);
      await page.goto("/sign-in#l=pub-1&s=sec-1");

      await expect(
        page.getByRole("heading", { level: 1, name: "Join Lakana as Kim Lindqvist" }),
      ).toBeFocused();
      expect(page.url()).not.toContain("sec-1");
      await expect(page.getByRole("button", { name: "Add a passkey" })).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await tabTo(page, page.getByRole("button", { name: "Sign in with Okta" }));
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
      expect(world.bodies.get("POST /api/v1/sign-in/oidc/start")).toEqual({
        purpose: "invite",
        link_id: "pub-1",
        secret: "sec-1",
      });
      expect(world.sent.filter((sent) => sent.includes("sec-1"))).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("the profile's link dialog", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page);
      await page.goto("/account");

      const section = page.getByRole("region", { name: "Sign-in" });
      await expect(section).toContainText(
        "You sign in with a passkey. You can sign in through Okta instead.",
      );
      expect(await violations(page)).toEqual([]);
      const open = page.getByRole("button", { name: "Use Okta to sign in" });
      await tabTo(page, open);
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Sign in through Okta from now on?" });
      await expect(dialog).toContainText("Your passkeys stay stored but no longer sign you in.");
      expect(await violations(page), "the dialog").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(open).toBeFocused();
      expect(world.sent.filter((sent) => sent.includes("/oidc/link"))).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("a linked person's profile, its passkeys and its tokens' window", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { linked: true });
      await page.goto("/account");

      const section = page.getByRole("region", { name: "Sign-in" });
      await expect(section).toContainText("You sign in through Okta.");
      await expect(section).toContainText(/Your API tokens work until .*2026/);
      const passkeys = page.getByRole("region", { name: "Passkeys" });
      await expect(passkeys).toContainText("ThinkPad fingerprint");
      // No Remove: the headers that sort are the only buttons.
      await expect(passkeys.locator("tbody").getByRole("button")).toHaveCount(0);
      expect(await violations(page)).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("Settings › Sign-in with the warning", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { unlinkedAdmins: 1 });
      await page.goto("/settings/sign-in");

      const section = page.getByRole("region", { name: "Single sign-on" });
      await expect(section).toContainText("https://example.okta.com/oauth2/default");
      await expect(section).toContainText(
        "Warning: Only one administrator can still sign in with a passkey.",
      );
      await expect(page.getByRole("textbox", { name: "Token window in days" })).toHaveValue("30");
      expect(await violations(page)).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("Unlink on a person", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page);
      await stubLinkedKim(page, world);
      await page.goto("/settings/access/people/kim");

      const panel = page.getByRole("region", { name: "Linked to Okta" });
      await expect(panel).toContainText("kim.lindqvist@example.org");
      expect(await violations(page)).toEqual([]);
      await tabTo(page, panel.getByRole("button", { name: "Unlink" }));
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Unlink kim from Okta?" });
      await expect(dialog).toContainText("To offboard someone, deactivate them instead.");
      expect(await violations(page), "the dialog").toEqual([]);
      await tabTo(page, dialog.getByRole("button", { name: "Unlink" }));
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 1, name: "kim" })).toBeFocused();
      await expect(panel).toBeHidden();
      expect(world.sent).toContain("DELETE /api/v1/principals/kim/oidc-link");
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("a stale link on a person, with Unlink", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page);
      await stubLinkedKim(page, world, true);
      await page.goto("/settings/access/people/kim");

      const panel = page.getByRole("region", {
        name: "Linked to a provider that is no longer configured",
      });
      await expect(panel).toContainText("kim was linked to old-idp.example.com");
      await expect(panel).toContainText("Personal API tokens work until");
      expect(await violations(page)).toEqual([]);
      await tabTo(page, panel.getByRole("button", { name: "Unlink" }));
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Unlink kim from old-idp.example.com?" });
      await expect(dialog).toContainText("To offboard someone, deactivate them instead.");
      await tabTo(page, dialog.getByRole("button", { name: "Unlink" }));
      await page.keyboard.press("Enter");
      await expect(panel).toBeHidden();
      expect(world.sent).toContain("DELETE /api/v1/principals/kim/oidc-link");
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("a linked person's Create token goes through the provider and back", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { linked: true });
      world.returnTo = "/account";
      let created = 0;
      await page.route(
        (url) => url.pathname === "/api/v1/principals/admin/tokens",
        (route) => {
          if (route.request().method() === "POST") {
            created += 1;
            return route.fulfill({
              status: 201,
              json: { key_id: "k1", name: "laptop", token: "lak_k1_secret" },
            });
          }
          return route.fulfill({ json: [] });
        },
      );
      await page.goto("/account");

      await page.getByRole("button", { name: "Create a token" }).click();
      await page.getByRole("textbox", { name: "Name" }).fill("laptop");
      await page.getByRole("button", { name: "Create", exact: true }).click();

      const note = page.getByText(
        "Confirmed with Okta. Do it again now; it won't ask for 15 minutes.",
      );
      await expect(note).toBeFocused();
      expect(new URL(page.url()).pathname).toBe("/account");
      expect(created).toBe(0);
      expect(world.bodies.get("POST /api/v1/sign-in/oidc/step-up/start")).toEqual({
        return_to: "/account",
      });
      expect(await violations(page), "the note").toEqual([]);

      const sentBefore = world.sent.length;
      await page.getByRole("button", { name: "Create a token" }).click();
      await page.getByRole("textbox", { name: "Name" }).fill("laptop");
      await page.getByRole("button", { name: "Create", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "Your new token laptop" })).toBeVisible();
      expect(created).toBe(1);
      expect(world.sent.slice(sentBefore).filter((sent) => sent.includes("/oidc/"))).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("nothing mentions a provider when it is off", async ({ page }) => {
      const errors = consoleErrors(page);
      const world = await stubProvider(page, { name: null, signedIn: false });
      await page.goto("/sign-in");
      await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
      await expect(page.locator("main")).not.toContainText(/provider|Okta/i);

      world.signedIn = true;
      for (const path of ["/account", "/settings/sign-in"]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(
          page
            .getByRole("region", { name: "Passkeys" })
            .or(page.getByRole("region", { name: "Session lengths" })),
        ).toBeVisible();
        await expect(page.locator("main"), path).not.toContainText(
          /identity provider|Okta|Single sign-on|Token window/,
        );
      }
      expect(world.sent.filter((sent) => sent.includes("/oidc/"))).toEqual([]);
      expect(unstubbed(world)).toEqual([]);
      expect(unexpected(errors)).toEqual([]);
    });

    test("at 320 px no sideways scroll with a 200-character provider name", async ({ page }) => {
      const name = `Corp‮${"single-sign-on-of-the-northern-region-".repeat(5)}`.slice(0, 200);
      await stubProvider(page, { name, signedIn: false });
      await page.setViewportSize({ width: 320, height: 640 });
      await page.goto("/sign-in");

      const button = page.getByRole("button", { name: /^Sign in with Corp/ });
      await expect(button).toContainText("U+202E");
      expect(await scrollsSideways(page)).toBe(false);
      const box = await button.boundingBox();
      expect(box === null ? 0 : box.x + box.width).toBeLessThanOrEqual(320);
      expect(await violations(page)).toEqual([]);
    });
  });
}
