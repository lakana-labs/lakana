/**
 * Stubs of sign-in through the organisation's identity provider (#32) for the browser checks:
 * the provider's answer, the flows' starts and finishes, and the provider's own page, which
 * answers a 303 back to the interface's return page as a real provider does after sign-in.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

import { ADMIN_SESSION } from "./session";

/** The provider's sign-in page the stubbed starts answer. */
export const PROVIDER_URL = "https://id.example.org/authorize";

/** The API paths these stubs answer, beside `STUBBED` of `session.ts`. */
export const OIDC_STUBBED = [
  "/api/v1/sign-in/oidc/start",
  "/api/v1/sign-in/oidc/finish",
  "/api/v1/sign-in/oidc/link/start",
  "/api/v1/sign-in/oidc/step-up/start",
  "/api/v1/sign-in/oidc/finish-in-session",
  "/api/v1/sign-in/links/describe",
  "/api/v1/principals/admin/passkeys",
  "/api/v1/principals/admin/sessions",
  "/api/v1/principals/admin",
  "/api/v1/principals/kim",
  "/api/v1/principals/kim/passkeys",
  "/api/v1/principals/kim/sessions",
  "/api/v1/principals/kim/oidc-link",
  "/api/v1/principals/kim/grants",
  "/api/v1/groups",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** What the stubbed server knows, changed by the flows. */
export interface ProviderWorld {
  /** Whether a browser session exists. */
  signedIn: boolean;
  /** Whether the administrator signs in through the provider. */
  linked: boolean;
  /** Until when the session's step-up lasts, or null. */
  stepUpUntil: string | null;
  /** The refusal the next finish answers, or null for a success. */
  refuse: string | null;
  /** The page the next finish names. */
  returnTo: string;
  /** Every request to the API or the provider, as `METHOD path`. */
  readonly sent: string[];
  /** The bodies of the API's writes, by `METHOD path`. */
  readonly bodies: Map<string, unknown>;
}

/** Options of {@link stubProvider}. */
export interface ProviderOptions {
  /** The provider's name; null for none (OIDC off). */
  readonly name?: string | null;
  /** Whether a session exists at the start. */
  readonly signedIn?: boolean;
  /** Whether the administrator signs in through the provider. */
  readonly linked?: boolean;
  /** The unlinked administrators Settings › Sign-in counts. */
  readonly unlinkedAdmins?: number;
}

/**
 * A refusal of the provider's sign-in, as the server answers it.
 *
 * @param reason - The reason.
 * @returns The answer.
 */
function refusal(reason: string): { status: number; json: unknown } {
  return {
    status: 403,
    json: { detail: `Refused: ${reason}.`, type: "OidcSignInRefused", reason },
  };
}

/**
 * Answers the identity provider's routes on top of `signInAsAdmin`'s, which the caller has
 * registered first (a later route wins), and records what was sent.
 *
 * @param page - The page.
 * @param options - The provider and the session.
 * @returns The stubbed server's state, which a test may change.
 */
export async function stubProvider(
  page: Page,
  options: ProviderOptions = {},
): Promise<ProviderWorld> {
  const name = options.name === undefined ? "Okta" : options.name;
  const world: ProviderWorld = {
    signedIn: options.signedIn ?? true,
    linked: options.linked ?? false,
    stepUpUntil: null,
    refuse: null,
    returnTo: "/",
    sent: [],
    bodies: new Map(),
  };
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/") || url.host === "id.example.org") {
      const key = `${request.method()} ${url.host === "id.example.org" ? url.href : url.pathname}`;
      world.sent.push(key);
      const body = request.postData();
      if (body !== null && body !== "") {
        world.bodies.set(key, JSON.parse(body));
      }
    }
  });
  const session = () => ({
    ...ADMIN_SESSION,
    via: world.linked ? "oidc" : "passkey",
    step_up_until: world.stepUpUntil,
    provider_linked: world.linked,
  });
  await page.route("**/api/v1/sign-in/session", (route) =>
    world.signedIn
      ? route.fulfill({ json: session() })
      : route.fulfill({ status: 401, json: { detail: "Sign in.", type: "AuthenticationError" } }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/sign-in/oidc",
    (route) => route.fulfill({ json: { enabled: name !== null, display_name: name } }),
  );
  for (const path of ["start", "link/start", "step-up/start"]) {
    await page.route(`**/api/v1/sign-in/oidc/${path}`, (route) =>
      route.fulfill({ json: { authorization_url: `${PROVIDER_URL}?flow=${path}` } }),
    );
  }
  // The provider signs the person in and sends the browser back, as a real one does.
  await page.route(`${PROVIDER_URL}**`, (route) => {
    const flow = new URL(route.request().url()).searchParams.get("flow") ?? "start";
    const origin = new URL(page.url()).origin;
    const back = flow === "start" ? "/sign-in/oidc" : "/sign-in/oidc/confirmed";
    return route.fulfill({ status: 303, headers: { location: `${origin}${back}` } });
  });
  await page.route("**/api/v1/sign-in/oidc/finish", (route) => {
    if (world.refuse !== null) {
      return route.fulfill(refusal(world.refuse));
    }
    world.signedIn = true;
    return route.fulfill({
      json: { name: "admin", csrf_token: "csrf-oidc", return_to: world.returnTo },
    });
  });
  await page.route("**/api/v1/sign-in/oidc/finish-in-session", (route) => {
    if (world.refuse !== null) {
      return route.fulfill(refusal(world.refuse));
    }
    world.stepUpUntil = new Date(Date.now() + 15 * 60_000).toISOString();
    return route.fulfill({
      json: { return_to: world.returnTo, step_up_until: world.stepUpUntil },
    });
  });
  await page.route(
    (url) => url.pathname === "/api/v1/settings/sign-in",
    (route) =>
      route.fulfill({
        json: {
          idle_timeout_minutes: 720,
          absolute_lifetime_minutes: 10080,
          bounds: {
            idle_timeout_minutes_min: 15,
            idle_timeout_minutes_max: 1440,
            absolute_lifetime_minutes_min: 60,
            absolute_lifetime_minutes_max: 43200,
            token_provider_window_days_min: 1,
            token_provider_window_days_max: 365,
          },
          token_provider_window_days: 30,
          provider:
            name === null
              ? null
              : {
                  display_name: name,
                  issuer: "https://example.okta.com/oauth2/default",
                  linked_people: 12,
                  unlinked_admins: options.unlinkedAdmins ?? 2,
                },
        },
      }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/principals/admin/passkeys",
    (route) =>
      route.fulfill({
        json: [
          {
            id: "p-1",
            name: "ThinkPad fingerprint",
            created_at: "2026-11-01T10:00:00Z",
            last_used_at: "2026-11-20T08:00:00Z",
            backup_eligible: false,
            disabled_reason: null,
            usable_here: true,
          },
        ],
      }),
  );
  await page.route(
    (url) => url.pathname.endsWith("/sessions") && url.pathname.startsWith("/api/v1/principals/"),
    (route) => route.fulfill({ json: [] }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/whoami",
    (route) =>
      route.fulfill({
        json: {
          name: "admin",
          type: "USER",
          task_token: null,
          ci_token: null,
          language: null,
          admin: true,
          has_access: true,
          may_deploy: true,
          secret_environments: ["dev", "prod"],
          manage_policy: true,
          reads_environments: true,
          instance_actions: ["MANAGE_ENVIRONMENT", "MANAGE_REPOSITORY"],
          managed_environments: ["dev", "prod"],
          picture: "e1",
          token_window_ends_at: world.linked ? "2026-12-25T09:00:00Z" : null,
        },
      }),
  );
  return world;
}

/**
 * Answers kim's page in Settings › Access › People: linked to the provider, with Unlink.
 *
 * @param page - The page.
 * @param world - The stubbed server's state, to record the unlink in.
 */
export async function stubLinkedKim(page: Page, world: ProviderWorld): Promise<void> {
  let unlinked = false;
  await page.route(
    (url) => url.pathname === "/api/v1/principals/kim",
    (route) =>
      route.fulfill({
        json: {
          name: "kim",
          type: "USER",
          is_active: true,
          picture: null,
          held_admin: false,
          provider_link: unlinked
            ? null
            : {
                issuer: "https://example.okta.com/oauth2/default",
                email: "kim.lindqvist@example.org",
                name: "Kim Lindqvist",
                last_sign_in_at: "2026-11-24T08:30:00Z",
                token_window_ends_at: "2026-12-24T08:30:00Z",
              },
        },
      }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/principals/kim/passkeys",
    (route) => route.fulfill({ json: [] }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/principals/kim/grants",
    (route) => route.fulfill({ json: [] }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/groups",
    (route) => route.fulfill({ json: [] }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/principals/kim/oidc-link",
    (route) => {
      unlinked = true;
      return route.fulfill({ status: 204 });
    },
  );
  world.stepUpUntil = new Date(Date.now() + 15 * 60_000).toISOString();
}

/**
 * Answers an invite link's description: kim joins, and the page offers the provider.
 *
 * @param page - The page.
 */
export async function stubInvite(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/links/describe", (route) =>
    route.fulfill({
      json: {
        kind: "invite",
        name: "kim",
        display_name: "Kim Lindqvist",
        state: "open",
        offers_provider: true,
      },
    }),
  );
}
