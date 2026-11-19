/**
 * Helpers for the end-to-end tests against a real lakana-server: links minted with the
 * operator's bootstrap token, and a virtual authenticator.
 *
 * @packageDocumentation
 */
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";

import AxeBuilder from "@axe-core/playwright";
import {
  type APIRequestContext,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type CDPSession,
  expect,
  type Page,
} from "@playwright/test";

import serverConfig from "../playwright.server.config";
import { CSRF_STORAGE_KEY } from "../src/api/client";

/** The bootstrap administrator's token, as `playwright.server.config.ts` fixes it. */
const BOOTSTRAP_TOKEN = "lakana_pat_e2ekey_e2esecret";

/** The origin the e2e server answers on, as `playwright.server.config.ts` fixes it. */
export const ORIGIN = "http://localhost:4174";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// React Aria's live announcer (a pending button speaks through it) keeps its message for 7 s and
// names it by the ids of the button's parts, which may be gone by then; it is the library's
// transient node, not the page.
const LIVE_ANNOUNCER = "[data-live-announcer]";

/**
 * Opens a browser that has never visited the server.
 *
 * @param browser - The browser.
 * @returns A page of a fresh context.
 */
export async function freshPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ baseURL: ORIGIN });
  return context.newPage();
}

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations, each with the elements it found.
 */
export async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
}

/**
 * Throws with the answer's status and body unless it succeeded.
 *
 * @param what - The request, as the error names it.
 * @param response - The answer.
 */
async function ensureOk(what: string, response: APIResponse): Promise<void> {
  if (!response.ok()) {
    throw new Error(`${what} answered ${String(response.status())}: ${await response.text()}`);
  }
}

/**
 * Asks the API as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param path - The API path.
 * @param body - The JSON body, if any.
 * @returns The answer's JSON.
 */
async function ask(request: APIRequestContext, path: string, body?: object): Promise<string> {
  const response = await request.post(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    ...(body === undefined ? {} : { data: body }),
  });
  await ensureOk(`POST ${path}`, response);
  return ((await response.json()) as { url: string }).url;
}

/**
 * Mints the link that signs the first administrator in. It works once, and only while the
 * administrator has no passkey.
 *
 * @param request - The request context.
 * @returns The link, with its secret after the `#`.
 */
export function setupLink(request: APIRequestContext): Promise<string> {
  return ask(request, "/api/v1/sign-in/setup-link");
}

/**
 * Mints the local sign-in link `lakana server start --local` opens: the first administrator's,
 * with their own token. It works once, for 60 seconds, and only offers a passkey.
 *
 * @param request - The request context.
 * @returns The link, with its secret after the `#`.
 */
export function localLink(request: APIRequestContext): Promise<string> {
  return ask(request, "/api/v1/sign-in/local-link");
}

/**
 * Mints an invite for a new person.
 *
 * @param request - The request context.
 * @param name - The name the person signs up as; unique in the run.
 * @returns The link, with its secret after the `#`.
 */
export function inviteLink(request: APIRequestContext, name: string): Promise<string> {
  return ask(request, "/api/v1/invites", { name });
}

/** What the web interface remembers for a person, as `/interface-state` answers it. */
export interface InterfaceState {
  /** Whether they saw the tour. */
  readonly tour_seen: boolean;
  /** Whether they hid Get started. */
  readonly get_started_hidden: boolean;
  /** The last update note they saw (a release version), if any. */
  readonly update_note_seen: string | null;
}

/**
 * Reads, and with `change` first changes, the bootstrap administrator's interface state, so
 * each test starts from the state it needs whatever ran before it. Only the administrator's:
 * the bootstrap token is theirs, and a person changes only their own.
 *
 * @param request - The request context.
 * @param change - The fields to change; without it, the state is only read.
 * @returns The state afterwards.
 */
export async function interfaceState(
  request: APIRequestContext,
  change?: Partial<InterfaceState>,
): Promise<InterfaceState> {
  const path = "/api/v1/principals/admin/interface-state";
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const response =
    change === undefined
      ? await request.get(path, { headers })
      : await request.patch(path, { headers, data: change });
  await ensureOk("interface-state", response);
  return (await response.json()) as InterfaceState;
}

/**
 * Records that the bootstrap administrator saw the tour, so the welcome does not open in a test
 * about something else.
 *
 * @param request - The request context.
 */
export async function skipTheTour(request: APIRequestContext): Promise<void> {
  await interfaceState(request, { tour_seen: true });
}

/**
 * Gives an invited person the ADMIN role on the instance, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param name - The person.
 */
export async function makeAdministrator(request: APIRequestContext, name: string): Promise<void> {
  const response = await request.post("/api/v1/grants", {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    data: { principal: name, role: "ADMIN", kind: "INSTANCE" },
  });
  await ensureOk("POST /api/v1/grants", response);
}

/**
 * Gives a person a role on an environment, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param principal - The person.
 * @param role - The role, such as `VIEWER`.
 * @param environment - The environment.
 */
export async function grantRole(
  request: APIRequestContext,
  principal: string,
  role: string,
  environment: string,
): Promise<void> {
  await create(request, "/api/v1/grants", { principal, role, kind: "ENVIRONMENT", environment });
}

/**
 * Signs the bootstrap administrator in with the local link, as `lakana server start --local`
 * does, and waits for Home. The passkey offer comes only while they have none; it is declined,
 * as adding one would end the setup link a test of another file needs.
 *
 * @param page - A page of a fresh context.
 * @param request - The request context.
 */
export async function signInLocally(page: Page, request: APIRequestContext): Promise<void> {
  await page.goto(await localLink(request));
  const landed = page.getByRole("heading", { level: 1, name: /^(Add a passkey|Home)$/ });
  await expect(landed).toBeVisible();
  if ((await landed.textContent()) === "Add a passkey") {
    await page.getByRole("button", { name: "Not now" }).click();
  }
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
}

/**
 * A name no earlier test of this run used.
 *
 * @param prefix - The start of the name.
 * @returns The name.
 */
export function uniqueName(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Adds a virtual authenticator (Chromium only) that holds resident keys and verifies users.
 *
 * @param context - The browser context.
 * @param page - A page of the context.
 * @returns The CDP session.
 */
export async function addAuthenticator(context: BrowserContext, page: Page): Promise<CDPSession> {
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  return cdp;
}

/**
 * Posts JSON as the bootstrap administrator and throws unless it succeeded.
 *
 * @param request - The request context.
 * @param path - The API path.
 * @param body - The JSON body.
 */
async function create(request: APIRequestContext, path: string, body: object): Promise<void> {
  const response = await request.post(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    data: body,
  });
  await ensureOk(`POST ${path}`, response);
}

/**
 * The directory of an environment's files: its `file://` root as {@link createEnvironment}
 * gives it, under `ui/.e2e-server/`, which the server's start clears.
 *
 * @param name - The environment's name.
 * @returns The directory's absolute path.
 */
function lakeRoot(name: string): string {
  return resolve(import.meta.dirname, "..", ".e2e-server", `lake-${name}`);
}

/**
 * Creates an environment whose files live under `ui/.e2e-server/`, which the server's start
 * clears.
 *
 * @param request - The request context.
 * @param name - The environment's name; unique in the run.
 * @returns Resolves once the server answered.
 */
export function createEnvironment(request: APIRequestContext, name: string): Promise<void> {
  return create(request, "/api/v1/environments", {
    name,
    storage: {
      scheme: "file",
      root: `file://${lakeRoot(name)}`,
      options: {},
    },
  });
}

/**
 * Writes a file into an environment's storage, as a person drops a file under `raw/` for an
 * external table to read. The directories are made as needed.
 *
 * @param environment - The environment, created by {@link createEnvironment}.
 * @param relative - The file's path under the environment's root, such as `raw/orders/a.csv`.
 * @param content - The file's text.
 */
export function writeRawFile(environment: string, relative: string, content: string): void {
  const file = resolve(lakeRoot(environment), relative);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, content);
}

/**
 * The path of an artifact's routes.
 *
 * @param environment - The environment.
 * @param artifact - The artifact's dotted name, such as `ml.model`; its namespace must exist.
 * @returns The path, without a trailing `/`.
 */
function artifactPath(environment: string, artifact: string): string {
  const dot = artifact.lastIndexOf(".");
  return `/api/v1/environments/${environment}/namespaces/${artifact.slice(0, dot)}/artifacts/${artifact.slice(dot + 1)}`;
}

/**
 * The manifest of an upload, as `lakana artifact upload` sends it
 * (`packages/lakana/src/lakana/_artifact_manifest.py`): a directory of files, sorted by path.
 *
 * @param files - Each file's text by its path.
 * @returns The manifest.
 */
function manifest(files: Readonly<Record<string, string>>): object {
  return {
    format: 1,
    is_directory: true,
    files: Object.keys(files)
      .sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))
      .map((path) => {
        const bytes = Buffer.from(files[path] ?? "");
        return {
          path,
          size: bytes.length,
          sha256: createHash("sha256").update(bytes).digest("hex"),
        };
      }),
  };
}

/**
 * Stores a version of an artifact as the bootstrap administrator, the way the SDK does: begin an
 * upload with the manifest, put each file the server is missing, and commit. The first upload
 * creates the artifact.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param artifact - The artifact's dotted name, such as `ml.model`; its namespace must exist.
 * @param files - Each file's text by its path in the version.
 * @returns The version's number.
 */
export async function uploadArtifact(
  request: APIRequestContext,
  environment: string,
  artifact: string,
  files: Readonly<Record<string, string>>,
): Promise<number> {
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const base = artifactPath(environment, artifact);
  const begun = await request.post(`${base}/uploads`, { headers, data: manifest(files) });
  await ensureOk(`POST ${base}/uploads`, begun);
  const { upload_id: id, missing } = (await begun.json()) as {
    upload_id: string;
    missing: string[];
  };
  for (const text of Object.values(files)) {
    const bytes = Buffer.from(text);
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    if (missing.includes(sha256)) {
      const path = `${base}/uploads/${id}/blobs/${sha256}`;
      await ensureOk(
        `PUT ${path}`,
        await request.put(path, {
          headers: { ...headers, "Content-Type": "application/octet-stream" },
          data: bytes,
        }),
      );
      missing.splice(missing.indexOf(sha256), 1);
    }
  }
  const committed = await request.post(`${base}/uploads/${id}/commit`, {
    headers,
    data: { sources: [] },
  });
  await ensureOk(`POST ${base}/uploads/${id}/commit`, committed);
  return ((await committed.json()) as { version: number }).version;
}

/**
 * The headers a request needs to act as the page's signed-in person: the page's CSRF token, and
 * the origin. The session cookie comes with the page's own request context.
 *
 * @param page - The page, signed in.
 * @returns The headers.
 */
async function sessionHeaders(page: Page): Promise<Record<string, string>> {
  const token = await page.evaluate((key) => localStorage.getItem(key), CSRF_STORAGE_KEY);
  return { "X-Lakana-CSRF": token ?? "", Origin: ORIGIN };
}

/**
 * Begins an upload of one small file as the signed-in page's person, with the page's session and
 * CSRF token, so that the upload is the person's own, and leaves it open.
 *
 * @param page - The page, signed in.
 * @param environment - The environment.
 * @param artifact - The artifact's dotted name, such as `ml.model`.
 * @returns The upload's id.
 */
export async function beginUploadAs(
  page: Page,
  environment: string,
  artifact: string,
): Promise<string> {
  const path = `${artifactPath(environment, artifact)}/uploads`;
  const response = await page.request.post(path, {
    headers: await sessionHeaders(page),
    data: manifest({ "notes.txt": "not finished" }),
  });
  await ensureOk(`POST ${path}`, response);
  return ((await response.json()) as { upload_id: string }).upload_id;
}

/**
 * Reads an artifact as the bootstrap administrator, to see whether it is still there and what
 * its versions are.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param artifact - The artifact's dotted name.
 * @returns The HTTP status of the artifact's read, and its versions (none once it is gone).
 */
export async function artifactState(
  request: APIRequestContext,
  environment: string,
  artifact: string,
): Promise<{ status: number; versions: { version: number; deleted_at: string | null }[] }> {
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const base = artifactPath(environment, artifact);
  const status = (await request.get(base, { headers })).status();
  if (status !== 200) {
    return { status, versions: [] };
  }
  const response = await request.get(`${base}/versions`, { headers });
  await ensureOk(`GET ${base}/versions`, response);
  return {
    status,
    versions: (await response.json()) as { version: number; deleted_at: string | null }[],
  };
}

/**
 * Lists the signed-in page's person's own open uploads of an artifact, with the page's session
 * and CSRF token.
 *
 * @param page - The page, signed in.
 * @param environment - The environment.
 * @param artifact - The artifact's dotted name.
 * @returns The uploads' ids.
 */
export async function uploadsAs(
  page: Page,
  environment: string,
  artifact: string,
): Promise<string[]> {
  const path = `/api/v1/environments/${environment}/artifacts/uploads?name=${encodeURIComponent(artifact)}`;
  const response = await page.request.get(path, { headers: await sessionHeaders(page) });
  await ensureOk(`GET ${path}`, response);
  return ((await response.json()) as { items: { upload_id: string }[] }).items.map(
    (item) => item.upload_id,
  );
}

/**
 * Creates a namespace; its parent must exist.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param path - The dotted path, such as `gold.sales`.
 * @returns Resolves once the server answered.
 */
export function createNamespace(
  request: APIRequestContext,
  environment: string,
  path: string,
): Promise<void> {
  return create(request, `/api/v1/environments/${environment}/namespaces`, { path });
}

/**
 * Creates a managed table.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @param columns - The columns' names; each is a nullable string.
 * @returns Resolves once the server answered.
 */
export function createTable(
  request: APIRequestContext,
  environment: string,
  namespace: string,
  name: string,
  columns: readonly string[],
): Promise<void> {
  return create(request, `/api/v1/environments/${environment}/namespaces/${namespace}/tables`, {
    name,
    schema: columns.map((column) => ({ name: column, type: "string", nullable: true })),
  });
}

/**
 * Drops a table, as the platform's own token does.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @returns Resolves once the server answered.
 */
export async function dropTable(
  request: APIRequestContext,
  environment: string,
  namespace: string,
  name: string,
): Promise<void> {
  const path = `/api/v1/environments/${environment}/namespaces/${namespace}/tables/${name}`;
  const response = await request.delete(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  await ensureOk(`DELETE ${path}`, response);
}

/**
 * Lets a person read one table.
 *
 * @param request - The request context.
 * @param principal - The person.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param table - The table's name.
 * @returns Resolves once the server answered.
 */
export function grantTableRead(
  request: APIRequestContext,
  principal: string,
  environment: string,
  namespace: string,
  table: string,
): Promise<void> {
  return create(request, "/api/v1/grants", {
    principal,
    action: "READ_TABLE",
    kind: "TABLE",
    environment,
    namespace,
    table,
  });
}

/**
 * Gives a person one right on an environment, or on a namespace of it.
 *
 * @param request - The request context.
 * @param principal - The person.
 * @param action - The right, such as `READ_NAMESPACE`.
 * @param environment - The environment.
 * @param namespace - The dotted namespace the right is bound to; the whole environment without.
 * @returns Resolves once the server answered.
 */
export function grantOn(
  request: APIRequestContext,
  principal: string,
  action: string,
  environment: string,
  namespace?: string,
): Promise<void> {
  return create(request, "/api/v1/grants", {
    principal,
    action,
    environment,
    ...(namespace === undefined ? { kind: "ENVIRONMENT" } : { kind: "NAMESPACE", namespace }),
  });
}

/**
 * Signs a newly invited person in with a passkey and waits for Home (Chromium only). `prepare`
 * runs once the person exists and before Home reads what they may do, so it is the place to
 * grant them something.
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @param prepare - Called with the person's name before the passkey step.
 * @returns The page, on Home, and the person's name.
 */
export async function invitedPerson(
  browser: Browser,
  request: APIRequestContext,
  prepare: (name: string) => Promise<void>,
): Promise<{ page: Page; name: string }> {
  const name = uniqueName("ada");
  const page = await freshPage(browser);
  await addAuthenticator(page.context(), page);
  await page.goto(await inviteLink(request, name));
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  // The person exists once the invite is redeemed.
  await prepare(name);
  await page.getByRole("button", { name: "Add a passkey" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  return { page, name };
}

/**
 * Waits for the welcome of a person with access who has never seen the tour, skips it, and waits
 * until the server has recorded that, so that a page loaded next does not show it again.
 *
 * @param page - The page, on Home.
 */
export async function skipTheWelcome(page: Page): Promise<void> {
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  await expect(welcome).toBeVisible();
  // The tour is remembered by the server; a page loaded before the answer would show it again.
  const remembered = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" && response.url().endsWith("/interface-state"),
  );
  await welcome.getByRole("button", { name: "Skip" }).click();
  await remembered;
  await expect(welcome).toBeHidden();
}

/**
 * Reads the state of a query run, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param queryId - The query's id.
 * @returns The state, such as `QUEUED` or `CANCELLED`.
 */
export async function queryStatus(
  request: APIRequestContext,
  environment: string,
  queryId: string,
): Promise<string> {
  const path = `/api/v1/environments/${environment}/queries/${queryId}`;
  const response = await request.get(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  await ensureOk(`GET ${path}`, response);
  return ((await response.json()) as { status: string }).status;
}

/**
 * Cancels every queued or running run of an environment, as the bootstrap administrator, so that a
 * test that started runs leaves none for the worker of a later test to pick up.
 *
 * @param request - The request context.
 * @param environment - The environment.
 */
export async function cancelRuns(request: APIRequestContext, environment: string): Promise<void> {
  const base = `/api/v1/environments/${environment}/runs`;
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const listed = await request.get(base, { headers });
  await ensureOk(`GET ${base}`, listed);
  const runs = (await listed.json()) as { id: string; status: string }[];
  for (const run of runs.filter((each) => each.status === "queued" || each.status === "running")) {
    const path = `${base}/${run.id}/cancel`;
    await ensureOk(`POST ${path}`, await request.post(path, { headers }));
  }
}

/**
 * Signs the bootstrap administrator in on Home without the welcome.
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @returns The page.
 */
export async function administrator(browser: Browser, request: APIRequestContext): Promise<Page> {
  await skipTheTour(request);
  const page = await freshPage(browser);
  await signInLocally(page, request);
  return page;
}

/**
 * Reads a table's status as the bootstrap administrator, to see whether it is still there.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @returns The HTTP status: 200 when the table exists, 404 when it does not.
 */
export async function tableStatus(
  request: APIRequestContext,
  environment: string,
  namespace: string,
  name: string,
): Promise<number> {
  const path = `/api/v1/environments/${environment}/namespaces/${namespace}/tables/${name}`;
  const response = await request.get(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  return response.status();
}

/**
 * Reads a namespace's status as the bootstrap administrator, to see whether it is still there.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param path - The dotted namespace.
 * @returns The HTTP status: 200 when the namespace exists, 404 when it does not.
 */
export async function namespaceStatus(
  request: APIRequestContext,
  environment: string,
  path: string,
): Promise<number> {
  const response = await request.get(`/api/v1/environments/${environment}/namespaces/${path}`, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  return response.status();
}

/**
 * Registers a repository through the API and records a project version of it that produced
 * tables, with no deployment after it, so those tables are orphans. A deploy needs a git host
 * and a worker, which this suite has neither of; the version row is one of three things written
 * straight into the server's SQLite file (`ui/.e2e-server/lakana.db`), and the API reads it
 * back as it would a deployed one.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param repository - The repository's name; unique in the run.
 * @param tables - The dotted names of the tables the project version produced.
 * @returns Resolves once the version is recorded.
 */
export async function recordOrphanedVersion(
  request: APIRequestContext,
  environment: string,
  repository: string,
  tables: readonly string[],
): Promise<void> {
  await create(request, "/api/v1/repositories", {
    name: repository,
    kind: "https",
    url: `https://github.com/acme/${repository}.git`,
  });
  const database = new DatabaseSync(resolve(import.meta.dirname, "..", ".e2e-server", "lakana.db"));
  try {
    const ids = (sql: string, name: string): string =>
      (database.prepare(sql).get(name) as { id: string }).id;
    database
      .prepare(
        `INSERT INTO job_versions (id, repository_id, environment_id, commit_sha, manifest_json,
           code_uri, code_sha256, created_at)
         VALUES (?, ?, ?, ?, ?, 's3://bucket/code.tar.gz', ?, '2026-11-01 08:00:00.000000')`,
      )
      .run(
        randomUUID().replaceAll("-", ""),
        ids("SELECT id FROM repositories WHERE name = ?", repository),
        ids("SELECT id FROM environments WHERE name = ?", environment),
        "a".repeat(40),
        JSON.stringify({
          version: 1,
          assets: tables.map((table) => ({ table, references: [] })),
          edges: [],
          external_inputs: [],
        }),
        "0".repeat(64),
      );
  } finally {
    database.close();
  }
}

/** A job of a deployed project, as {@link recordDeployedJob} records it. */
export interface DeployedJob {
  /** The job's name. */
  readonly name: string;
  /** The dotted table the project builds and the job runs. */
  readonly table: string;
  /** The job's cron expression, in `Europe/Berlin`. */
  readonly cron: string;
  /** The name of the secret that holds the job's Slack webhook. */
  readonly secret: string;
}

/**
 * The form SQLAlchemy writes a UTC time in on SQLite: no zone, six digits of a second.
 *
 * @param at - The time.
 * @returns The text.
 */
function sqliteTime(at: Date): string {
  return `${at.toISOString().slice(0, 23).replace("T", " ")}000`;
}

/** One project version of a repository, as {@link recordDeployHistory} records it. */
export interface DeployedVersion {
  /** The full commit the version was deployed from. */
  readonly commit: string;
  /** The jobs the version declares, each with the table it builds and runs. */
  readonly jobs: readonly DeployedJob[];
}

/**
 * Registers a repository through the API and records it as deployed in an environment with one
 * job: a project version whose manifest builds one table and declares the job (selecting that
 * table, on a schedule, notifying Slack through a secret), its succeeded deploy, the
 * environment's pointer to that version, and the job's schedule row, as a deploy would leave
 * them. It is {@link recordDeployHistory} of one version.
 *
 * @param request - The request context.
 * @param environment - The environment; unprotected.
 * @param repository - The repository's name; unique in the run.
 * @param job - The job.
 * @returns Resolves once the deploy is recorded.
 */
export async function recordDeployedJob(
  request: APIRequestContext,
  environment: string,
  repository: string,
  job: DeployedJob,
): Promise<void> {
  await recordDeployHistory(request, environment, repository, [
    { commit: "b".repeat(40), jobs: [job] },
  ]);
}

/**
 * Registers a repository through the API and records its deploys to an environment, oldest
 * first: for each version a project version whose manifest builds its jobs' tables and declares
 * its jobs (each selecting its table, on a schedule, notifying Slack through a secret) and a
 * succeeded deploy of its commit that made it, an hour apart; the last version is current, with
 * the environment's pointer to it and its jobs' schedule rows. A deploy needs a git host and a
 * worker, which this suite has neither of; like {@link recordOrphanedVersion}, the rows are
 * written straight into the server's SQLite file (`ui/.e2e-server/lakana.db`), the columns those
 * of `JobVersion`, `DeployRun`, `EnvironmentDeployment` (`models/deploy.py`) and `JobSchedule`
 * (`models/job.py`), and the API reads them back as it would real deploys'. The deploys name no
 * person (as one whose account was deleted), and no run principal is bound, so a start by hand in
 * an unprotected environment runs as the person who starts it.
 *
 * @param request - The request context.
 * @param environment - The environment; unprotected.
 * @param repository - The repository's name; unique in the run.
 * @param versions - The versions, oldest first; at least one.
 * @returns Resolves once the deploys are recorded.
 */
export async function recordDeployHistory(
  request: APIRequestContext,
  environment: string,
  repository: string,
  versions: readonly DeployedVersion[],
): Promise<void> {
  await create(request, "/api/v1/repositories", {
    name: repository,
    kind: "https",
    url: `https://github.com/acme/${repository}.git`,
  });
  const database = new DatabaseSync(resolve(import.meta.dirname, "..", ".e2e-server", "lakana.db"));
  try {
    const ids = (sql: string, name: string): string =>
      (database.prepare(sql).get(name) as { id: string }).id;
    const repositoryId = ids("SELECT id FROM repositories WHERE name = ?", repository);
    const environmentId = ids("SELECT id FROM environments WHERE name = ?", environment);
    const now = Date.now();
    let versionId = "";
    versions.forEach((version, index) => {
      versionId = randomUUID().replaceAll("-", "");
      const at = sqliteTime(new Date(now - (versions.length - index) * 3_600_000));
      const manifest = {
        version: 1,
        assets: version.jobs.map((job) => ({ table: job.table, references: [] })),
        edges: [],
        external_inputs: [],
        jobs: Object.fromEntries(
          version.jobs.map((job) => [
            job.name,
            {
              select: [job.table],
              schedule: { cron: job.cron, timezone: "Europe/Berlin" },
              notify: { webhook: job.secret, format: "slack" },
            },
          ]),
        ),
      };
      database
        .prepare(
          `INSERT INTO job_versions (id, repository_id, environment_id, commit_sha, manifest_json,
             code_uri, code_sha256, created_at)
           VALUES (?, ?, ?, ?, ?, 's3://bucket/code.tar.gz', ?, ?)`,
        )
        .run(
          versionId,
          repositoryId,
          environmentId,
          version.commit,
          JSON.stringify(manifest),
          "0".repeat(64),
          at,
        );
      // The enums are stored by their names (`DeployKind`, `DeployStatus`).
      database
        .prepare(
          `INSERT INTO deploy_runs (id, environment_id, repository_id, principal_id, kind,
             requested_ref, resolved_sha, status, job_version_id, created_at, started_at,
             finished_at)
           VALUES (?, ?, ?, NULL, 'DEPLOY', 'main', ?, 'SUCCEEDED', ?, ?, ?, ?)`,
        )
        .run(
          randomUUID().replaceAll("-", ""),
          environmentId,
          repositoryId,
          version.commit,
          versionId,
          at,
          at,
          at,
        );
    });
    database
      .prepare(
        `INSERT INTO environment_deployments (environment_id, repository_id,
           current_job_version_id, run_as_principal_id, updated_at)
         VALUES (?, ?, ?, NULL, ?)`,
      )
      .run(environmentId, repositoryId, versionId, sqliteTime(new Date(now)));
    // The scheduler would work the next time out; any time ahead does for a server with no worker.
    for (const job of versions.at(-1)?.jobs ?? []) {
      database
        .prepare(
          `INSERT INTO job_schedules (id, environment_id, repository_id, job, next_fire_at, paused)
           VALUES (?, ?, ?, ?, ?, 0)`,
        )
        .run(
          randomUUID().replaceAll("-", ""),
          environmentId,
          repositoryId,
          job.name,
          sqliteTime(new Date(now + 86_400_000)),
        );
    }
  } finally {
    database.close();
  }
}

/**
 * Creates a connection through the API as the bootstrap administrator, which needs no passkey
 * touch.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param body - The connection: `name`, `kind`, `address`, `settings`, and its secret.
 * @returns Resolves once the server answered.
 */
export function createConnection(
  request: APIRequestContext,
  environment: string,
  body: object,
): Promise<void> {
  return create(request, `/api/v1/environments/${environment}/connections`, body);
}

/**
 * Reads a connection's status as the bootstrap administrator, to see whether it is still there.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param name - The connection.
 * @returns The HTTP status: 200 when the connection exists, 404 when it does not.
 */
export async function connectionStatus(
  request: APIRequestContext,
  environment: string,
  name: string,
): Promise<number> {
  const response = await request.get(`/api/v1/environments/${environment}/connections/${name}`, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  return response.status();
}

/**
 * Moves a person's last passkey touch an hour into the past on every open session, so the next
 * action that needs a recent touch meets `StepUpRequired`, as after 15 minutes of work without
 * one. The server has no way to age a touch, so this, like {@link recordOrphanedVersion}, writes
 * straight into its SQLite file; the server reads the session from it on every request.
 *
 * @param name - The person's name.
 */
export function ageThePasskeyTouch(name: string): void {
  const anHourAgo = sqliteTime(new Date(Date.now() - 3_600_000));
  const database = new DatabaseSync(resolve(import.meta.dirname, "..", ".e2e-server", "lakana.db"));
  try {
    const changed = database
      .prepare(
        `UPDATE browser_sessions SET last_touch_at = ?
         WHERE ended_at IS NULL
           AND principal_id = (SELECT id FROM principals WHERE name = ?)`,
      )
      .run(anHourAgo, name);
    if (Number(changed.changes) === 0) {
      throw new Error(`no open session of ${name} to age`);
    }
  } finally {
    database.close();
  }
}

/** What the page says for the server's 409 `NoPasskeyError`: the account has no passkey to touch. */
export const NO_PASSKEY =
  "Your account has no passkey yet, and this action needs a passkey touch. Add one first under Passkeys and sessions, in the menu under your name, then try again.";

/**
 * The saved queries a signed-in page's person sees in an environment, read with the page's
 * session and CSRF token.
 *
 * @param page - The page, signed in.
 * @param environment - The environment.
 * @returns The answer's status, and the names it lists (none for a refusal).
 */
export async function savedQueriesAs(
  page: Page,
  environment: string,
): Promise<{ status: number; names: string[] }> {
  const response = await page.request.get(`/api/v1/saved-queries?environment=${environment}`, {
    headers: await sessionHeaders(page),
  });
  if (!response.ok()) {
    return { status: response.status(), names: [] };
  }
  const { items } = (await response.json()) as { items: { name: string }[] };
  return { status: response.status(), names: items.map((item) => item.name) };
}

/**
 * How many workers the server counts as alive, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @returns The count of workers whose heartbeat is younger than the lease.
 */
async function workersAlive(request: APIRequestContext): Promise<number> {
  const response = await request.get("/api/v1/status", {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  await ensureOk("GET /api/v1/status", response);
  return ((await response.json()) as { workers_alive: number }).workers_alive;
}

/**
 * Starts a worker on the e2e server's database, with the server's own settings from
 * `playwright.server.config.ts`, and waits until the server counts it alive. The suite runs no
 * worker otherwise (Home's status box, a preview that stays queued); only a file whose queries
 * must run starts one, in its `beforeAll`, and stops it in its `afterAll`. Stopped with SIGTERM,
 * the worker drains and removes its own heartbeat, so the server counts none again.
 *
 * `uv` starts `lakana-worker` as its child, so both run in a process group of their own
 * (`detached`), and every signal goes to the group: a SIGKILL after 30 s, or when the runner exits
 * without stopping it (an aborted run), ends the worker too, not only `uv`. A worker left behind
 * would take the next run's queued queries.
 *
 * @param request - The request context.
 * @returns A function that stops the worker and resolves once it has exited.
 * @throws {Error} when the worker cannot start, exits or is not alive within 90 s, with the end
 *   of its log.
 */
export async function startWorker(request: APIRequestContext): Promise<() => Promise<void>> {
  const server = serverConfig.webServer;
  const settings = (Array.isArray(server) ? server[0] : server)?.env ?? {};
  const worker = spawn("uv", ["run", "--frozen", "--python", "3.14", "lakana-worker"], {
    cwd: resolve(import.meta.dirname, "..", ".."),
    env: { ...process.env, ...settings },
    stdio: ["ignore", "ignore", "pipe"],
    detached: true,
  });
  let log = "";
  worker.stderr.on("data", (chunk: Buffer) => {
    log = `${log}${chunk.toString()}`.slice(-4_000);
  });
  // A spawn that fails (no `uv`) emits "error" and never "exit".
  let failed: Error | null = null;
  const exited = new Promise<void>((done) => {
    worker.once("exit", () => {
      done();
    });
    worker.once("error", (error) => {
      failed = error;
      done();
    });
  });
  const group = worker.pid;
  const signal = (name: NodeJS.Signals): void => {
    if (group === undefined) {
      return;
    }
    try {
      process.kill(-group, name);
    } catch {
      // The group has gone already.
    }
  };
  const reap = (): void => {
    signal("SIGKILL");
  };
  process.once("exit", reap);
  const stop = async (): Promise<void> => {
    signal("SIGTERM");
    const hard = setTimeout(reap, 30_000);
    await exited;
    clearTimeout(hard);
    // `uv` waits for its child, but a child it left would still be in the group.
    reap();
    process.off("exit", reap);
  };
  try {
    await expect
      .poll(
        () => {
          if (failed !== null) {
            throw new Error(`The worker could not start: ${failed.message}`);
          }
          if (worker.exitCode !== null) {
            throw new Error(`The worker stopped with ${String(worker.exitCode)}: ${log}`);
          }
          return workersAlive(request);
        },
        { timeout: 90_000, intervals: [500] },
      )
      .toBeGreaterThan(0);
  } catch (error) {
    await stop();
    throw error;
  }
  return stop;
}

/**
 * Gives a person one action on the whole instance, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param principal - The person.
 * @param action - The action, such as `MANAGE_POLICY`.
 * @returns Resolves once the server answered.
 */
export function grantInstanceAction(
  request: APIRequestContext,
  principal: string,
  action: string,
): Promise<void> {
  return create(request, "/api/v1/grants", { principal, action, kind: "INSTANCE" });
}

/**
 * Creates a row filter, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param body - The row filter, as `POST /api/v1/row-filters` takes it.
 * @returns Resolves once the server answered.
 */
export function createRowFilter(request: APIRequestContext, body: object): Promise<void> {
  return create(request, "/api/v1/row-filters", body);
}

/**
 * Plans and applies an access file, as the bootstrap administrator, the way `lakana access
 * plan` and `apply` do.
 *
 * @param request - The request context.
 * @param text - The file.
 */
export async function applyAccessFile(request: APIRequestContext, text: string): Promise<void> {
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const planned = await request.post("/api/v1/access/plan", { headers, data: { text } });
  await ensureOk("POST /api/v1/access/plan", planned);
  const { fingerprint, problems } = (await planned.json()) as {
    fingerprint: string;
    problems: string[];
  };
  if (problems.length > 0) throw new Error(problems.join("\n"));
  const applied = await request.post("/api/v1/access/apply", {
    headers,
    data: { text, plan_fingerprint: fingerprint },
  });
  await ensureOk("POST /api/v1/access/apply", applied);
}

/**
 * Registers a repository, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param body - The repository, as `POST /api/v1/repositories` takes it.
 * @returns Resolves once the server answered.
 */
export function createRepository(request: APIRequestContext, body: object): Promise<void> {
  return create(request, "/api/v1/repositories", body);
}
