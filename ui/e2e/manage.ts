/**
 * Stubs and helpers for the end-to-end tests of table actions and the catalog's views: the
 * lists of expiring tables and orphans, the dialogs' routes, a table a project defines, and the
 * step-up pair a drop asks for.
 *
 * @packageDocumentation
 */
import { generateKeyPairSync } from "node:crypto";

import type { BrowserContext, Page, Request } from "@playwright/test";

import { signInAsAdmin, STUBBED, TABLE_DETAIL, TABLE_PATH, freshTouch } from "./session";

/** Where the browser reaches the preview server: passkeys need a domain, never an address. */
export const ORIGIN = "http://localhost:4173";

/** The path of `prod`. */
const ENV_PATH = "/api/v1/environments/prod";

/** The path of the dropped tables of `gold.sales`, which a drop's landing page reads. */
const NAMESPACE_DROPPED = `${ENV_PATH}/namespaces/gold.sales/dropped-tables`;

/** The paths this file's stubs answer, beside those of `session.ts`. */
export const MANAGE_STUBBED = [
  ...STUBBED,
  `${ENV_PATH}/expiring-tables`,
  `${ENV_PATH}/orphans`,
  NAMESPACE_DROPPED,
  `${ENV_PATH}/namespaces/gold.sales/tables/fact_returns/undrop`,
  `${TABLE_PATH}/maintenance`,
  `${TABLE_PATH}/retention`,
  `${TABLE_PATH}/rebuild`,
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** A table that expires in the future, and one whose day has passed. */
const EXPIRING = [
  {
    name: "late_arrival",
    namespace: "gold",
    uuid: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
    source: "table",
    days: 30,
    on: null,
    never: false,
    set_by: "ana",
    set_at: "2026-10-01T08:00:00Z",
    last_activity_at: "2026-10-20T06:00:00Z",
    expires_at: "2099-01-01T06:00:00Z",
    kept: null,
  },
  {
    name: "old_totals",
    namespace: "gold.sales",
    uuid: "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a",
    source: "environment",
    days: 14,
    on: null,
    never: false,
    set_by: null,
    set_at: null,
    last_activity_at: "2026-09-01T06:00:00Z",
    expires_at: "2026-10-01T06:00:00Z",
    kept: null,
  },
];

/** One orphan that exists, one already dropped, and one the person may not read. */
const ORPHANS = {
  orphans: [
    {
      repository: "analytics",
      table: "silver.clean",
      kind: "TABLE",
      exists: true,
      actions: ["drop"],
    },
    {
      repository: "analytics",
      table: "gold.sales.fact_returns",
      kind: "TABLE",
      exists: false,
      actions: ["drop"],
    },
  ],
  hidden: 1,
  next_cursor: null,
};

/** What a rebuild queued. */
const REBUILT = {
  table: "gold.sales.fact_sales",
  run_id: "run-42",
  repository: "analytics",
  path: "models/gold/fact_sales.sql",
};

/** The retention `fact_sales` has. */
const RETENTION = {
  name: "fact_sales",
  namespace: "gold.sales",
  uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
  source: "table",
  days: 30,
  on: null,
  never: false,
  set_by: "ana",
  set_at: "2026-11-01T08:00:00Z",
  last_activity_at: "2026-11-12T06:00:00Z",
  expires_at: "2026-12-12T06:00:00Z",
  kept: null,
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
 * Gives the browser a virtual authenticator holding the administrator's passkey, so a step-up
 * touch answers without a person (Chromium only).
 *
 * @param context - The browser context.
 * @param page - The page.
 */
export async function addAdminPasskey(context: BrowserContext, page: Page): Promise<void> {
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
}

/**
 * Answers the step-up pair, the options and the touch, as the server does once the virtual
 * authenticator has signed.
 *
 * @param page - The page.
 * @returns Whether the touch was sent yet.
 */
export async function stubStepUpCeremony(page: Page): Promise<() => boolean> {
  let touched = false;
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
  return () => touched;
}

/**
 * Collects the mutating requests to one path and to the step-up pair, in order.
 *
 * @param page - The page.
 * @param target - The path whose writes are collected.
 * @returns The requests, filled as they are sent, as `METHOD path`.
 */
export function mutatingRequests(page: Page, target: string): string[] {
  const sent: string[] = [];
  page.on("request", (request: Request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() !== "GET" &&
      (path === target || path.startsWith("/api/v1/sign-in/step-up"))
    ) {
      sent.push(`${request.method()} ${path}`);
    }
  });
  return sent;
}

/**
 * Makes the table's DELETE ask for a passkey touch once, answers the touch, and then lets the
 * delete through.
 *
 * @param page - The page.
 * @returns The mutating requests that reached the stubs, in order, as `METHOD path`.
 */
export async function stubStepUpDrop(page: Page): Promise<string[]> {
  const sent = mutatingRequests(page, TABLE_PATH);
  const touched = await stubStepUpCeremony(page);
  await page.route(
    (url) => url.pathname === TABLE_PATH,
    (route) =>
      route.request().method() !== "DELETE"
        ? route.fallback()
        : touched()
          ? route.fulfill({ status: 204 })
          : route.fulfill({
              status: 403,
              json: { type: "StepUpRequired", detail: "Confirm with your passkey." },
            }),
  );
  return sent;
}

/**
 * Makes `fact_sales` a table a deployed project defines: Drop is offered but disabled, and
 * Rebuild from git is offered.
 *
 * @param page - The page.
 */
export async function stubGitOwned(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === TABLE_PATH,
    (route) =>
      route.request().method() !== "GET"
        ? route.fallback()
        : route.fulfill({
            json: {
              ...TABLE_DETAIL,
              defined_by: { repository: "analytics", path: "models/gold/fact_sales.sql" },
              actions: ["drop", "rebuild", "maintain", "set_retention"],
            },
          }),
  );
}

/**
 * Signs in as an administrator and answers what the catalog's views and the table's dialogs
 * read: expiring tables, orphans, the dropped tables of a namespace, an undrop, maintenance,
 * retention and a rebuild.
 *
 * @param page - The page.
 */
export async function stubManagement(page: Page): Promise<void> {
  await signInAsAdmin(page);
  await freshTouch(page);
  await page.route(
    (url) => url.pathname === `${ENV_PATH}/expiring-tables`,
    (route) => route.fulfill({ json: EXPIRING }),
  );
  await page.route(
    (url) => url.pathname === `${ENV_PATH}/orphans`,
    (route) => route.fulfill({ json: ORPHANS }),
  );
  await page.route(
    (url) => url.pathname === NAMESPACE_DROPPED,
    (route) =>
      route.fulfill({
        json: [
          {
            name: "fact_sales",
            namespace: "gold.sales",
            uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
            kind: "TABLE",
            dropped_at: "2026-11-15T09:30:00Z",
            purge_after: "2026-11-22T09:30:00Z",
            restorable: true,
            actions: ["undrop"],
          },
          {
            name: "fact_returns",
            namespace: "gold.sales",
            uuid: "5c1d7e90-2f3a-4b6c-9d8e-0a1b2c3d4e5f",
            kind: "TABLE",
            dropped_at: "2026-11-14T09:30:00Z",
            purge_after: "2026-11-21T09:30:00Z",
            restorable: true,
            actions: ["undrop"],
          },
        ],
      }),
  );
  await page.route(
    (url) => url.pathname === `${ENV_PATH}/namespaces/gold.sales/tables/fact_returns/undrop`,
    (route) => route.fulfill({ json: { ...TABLE_DETAIL, name: "fact_returns", kind: "TABLE" } }),
  );
  await page.route(
    (url) => url.pathname === `${TABLE_PATH}/maintenance`,
    (route) => route.fulfill({ status: 202, json: { query_id: "q-77" } }),
  );
  await page.route(
    (url) => url.pathname === `${TABLE_PATH}/retention`,
    (route) => route.fulfill({ json: RETENTION }),
  );
  await page.route(
    (url) => url.pathname === `${TABLE_PATH}/rebuild`,
    (route) => route.fulfill({ status: 202, json: REBUILT }),
  );
}
