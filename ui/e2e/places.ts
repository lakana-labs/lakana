/**
 * Stubs for the end-to-end tests of the Catalog tree's place menu: a tree whose places say what
 * the administrator may do, the three writes (New namespace, Delete namespace, New external
 * table), a protected environment, and the step-up pair a namespace's delete asks for.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

import { mutatingRequests, stubStepUpCeremony } from "./manage";
import { signInAsAdmin, STUBBED } from "./session";

/** The path of `prod`. */
const PROD = "/api/v1/environments/prod";

/** The path of `dev`, which these stubs make protected. */
const DEV = "/api/v1/environments/dev";

/** The path of the empty namespace `prod.bronze`, the one Delete namespace is enabled on. */
export const BRONZE_PATH = `${PROD}/namespaces/bronze`;

/** Where New external table registers a table in `prod.gold.sales` and in `dev.gold`. */
const EXTERNAL = [
  `${PROD}/namespaces/gold.sales/tables/external`,
  `${DEV}/namespaces/gold/tables/external`,
];

/** The listing of `raw/` in `prod`, which Browse… reads. */
const RAW = `${PROD}/raw`;

/** The folders of `prod`'s `raw/` and what each holds, as the server lists them. */
const LAKE: Readonly<Record<string, readonly unknown[]>> = {
  raw: [
    { name: "events.csv", kind: "file", size: 2048, claimed_by: null },
    { name: "feeds", kind: "folder", size: null, claimed_by: null },
    { name: "orders", kind: "folder", size: null, claimed_by: { table: "bronze.orders" } },
  ],
  "raw/feeds": [
    { name: "2026-11-01.csv", kind: "file", size: 1200, claimed_by: null },
    { name: "2026-11-02.csv", kind: "file", size: 1300, claimed_by: null },
    { name: "archive", kind: "folder", size: null, claimed_by: null },
  ],
};

/** The path of the list, which `session.ts` answers by its query. */
const TABLES = "/api/v1/tables";

/** The paths this file's stubs answer, beside those of `session.ts`. */
export const PLACES_STUBBED = [
  ...STUBBED,
  DEV,
  `${PROD}/namespaces`,
  `${DEV}/namespaces`,
  BRONZE_PATH,
  `${PROD}/namespaces/bronze.landing`,
  ...EXTERNAL,
  RAW,
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** What an administrator may do on each namespace. */
const PLACE_ACTIONS = ["create_namespace", "create_external_table", "drop"];

/** A namespace of the tree, as the server sends it. */
interface TreeNamespace {
  readonly path: string;
  readonly count: number;
  readonly actions: readonly string[];
}

/**
 * The tree's namespaces by environment, as at the start of each test: `prod.bronze` is empty, so
 * it is the one place Delete namespace is enabled on.
 *
 * @returns The namespaces of `dev` and of `prod`.
 */
function startingTree(): Record<string, TreeNamespace[]> {
  return {
    dev: [{ path: "gold", count: 1, actions: PLACE_ACTIONS }],
    prod: [
      { path: "gold", count: 3, actions: PLACE_ACTIONS },
      { path: "gold.sales", count: 1, actions: PLACE_ACTIONS },
      { path: "bronze", count: 0, actions: PLACE_ACTIONS },
    ],
  };
}

/**
 * Signs in as an administrator and answers the place menu's routes like a server would: a
 * namespace created or deleted shows in, or leaves, the tree read next; `dev` is protected, so
 * its forms ask for a reason. New external table answers the server's refusal for a location
 * with no matching file: registering one, and the table page after it, is the real-server
 * suite's. Browse… lists a small `raw/` in `prod`.
 *
 * @param page - The page.
 */
export async function stubPlaces(page: Page): Promise<void> {
  await signInAsAdmin(page);
  const namespaces = startingTree();
  const created = new Set<string>();
  await page.route(
    (url) => url.pathname === "/api/v1/tables/tree",
    (route) => {
      const environments = Object.entries(namespaces).map(([name, nodes]) => ({
        name,
        count: nodes.reduce((sum, node) => sum + node.count, 0),
        actions: ["create_namespace"],
        namespaces: nodes,
      }));
      return route.fulfill({
        json: {
          count: environments.reduce((sum, environment) => sum + environment.count, 0),
          environments,
        },
      });
    },
  );
  await page.route(
    (url) => url.pathname === DEV,
    (route) => route.fulfill({ json: { name: "dev", is_protected: true } }),
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/(dev|prod)\/namespaces$/.test(url.pathname),
    async (route) => {
      const environment = new URL(route.request().url()).pathname.split("/")[4] ?? "";
      const { path } = route.request().postDataJSON() as { path: string };
      namespaces[environment]?.push({ path, count: 0, actions: PLACE_ACTIONS });
      created.add(path);
      await route.fulfill({ status: 201, json: { path, properties: {} } });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/prod\/namespaces\/[^/]+$/.test(url.pathname),
    async (route) => {
      if (route.request().method() !== "DELETE") {
        await route.fallback();
        return;
      }
      const path = decodeURIComponent(new URL(route.request().url()).pathname.split("/")[6] ?? "");
      namespaces.prod = (namespaces.prod ?? []).filter((node) => node.path !== path);
      await route.fulfill({ status: 204 });
    },
  );
  await page.route(
    (url) => EXTERNAL.includes(url.pathname),
    (route) => {
      const { location_pattern: pattern } = route.request().postDataJSON() as {
        location_pattern: string;
      };
      return route.fulfill({
        status: 404,
        json: { type: "NotFound", detail: `No file matches '${pattern}'.` },
      });
    },
  );
  await page.route(
    (url) => url.pathname === RAW,
    (route) => {
      const path = new URL(route.request().url()).searchParams.get("path") ?? "raw";
      return route.fulfill({ json: { path, items: LAKE[path] ?? [], next_cursor: null } });
    },
  );
  await page.route(
    (url) => url.pathname === TABLES,
    async (route) => {
      const namespace = new URL(route.request().url()).searchParams.get("namespace");
      if (namespace !== null && created.has(namespace)) {
        await route.fulfill({ json: { items: [], next_cursor: null, capped: false } });
        return;
      }
      await route.fallback();
    },
  );
}

/**
 * Makes the DELETE of `prod.bronze` ask for a passkey touch once, answers the touch, and then
 * lets the delete through to {@link stubPlaces}. Called after it.
 *
 * @param page - The page.
 * @returns The mutating requests that reached the stubs, in order, as `METHOD path`.
 */
export async function stubStepUpDelete(page: Page): Promise<string[]> {
  const sent = mutatingRequests(page, BRONZE_PATH);
  const touched = await stubStepUpCeremony(page);
  await page.route(
    (url) => url.pathname === BRONZE_PATH,
    (route) =>
      route.request().method() !== "DELETE" || touched()
        ? route.fallback()
        : route.fulfill({
            status: 403,
            json: { type: "StepUpRequired", detail: "Confirm with your passkey." },
          }),
  );
  return sent;
}
