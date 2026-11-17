/**
 * Helpers for the end-to-end tests against a real lakana-server: links minted with the
 * operator's bootstrap token, and a virtual authenticator.
 *
 * @packageDocumentation
 */
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
 * and a worker, which this suite has neither of; the version row is one of two things written
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

/**
 * Moves a person's last passkey touch an hour into the past on every open session, so the next
 * action that needs a recent touch meets `StepUpRequired`, as after 15 minutes of work without
 * one. The server has no way to age a touch, so this, like {@link recordOrphanedVersion}, writes
 * straight into its SQLite file; the server reads the session from it on every request.
 *
 * @param name - The person's name.
 */
export function ageThePasskeyTouch(name: string): void {
  // The form SQLAlchemy writes a UTC time in on SQLite: no zone, six digits of a second.
  const anHourAgo = `${new Date(Date.now() - 3_600_000).toISOString().slice(0, 23).replace("T", " ")}000`;
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
