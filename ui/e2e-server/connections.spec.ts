import { expect, type Page, test } from "@playwright/test";

import {
  ageThePasskeyTouch,
  connectionStatus,
  createConnection,
  createEnvironment,
  grantOn,
  invitedPerson,
  makeAdministrator,
  skipTheWelcome,
  uniqueName,
  violations,
} from "./helpers";

// The server runs no worker, so a test of a connection stays queued: what these tests see is the
// request and its first answer, which is all the screen shows until a worker takes it.

/** The password the tests type; it must appear in no page and in no request but the create. */
const PASSWORD = "p4ss-w0rd-of-the-e2e";

/**
 * Collects the requests that carry a text, with their method and path.
 *
 * @param page - The page.
 * @param text - The text to look for in a request's body and address.
 * @returns The requests, filled as they are sent, as `METHOD path`.
 */
function carrying(page: Page, text: string): string[] {
  const found: string[] = [];
  page.on("request", (request) => {
    if ((request.postData() ?? "").includes(text) || request.url().includes(text)) {
      found.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  return found;
}

/**
 * Collects the mutating requests of the Connections screen, with the step-up pair, as
 * `METHOD path` with the environment's name left out.
 *
 * @param page - The page.
 * @param environment - The environment, taken out of the paths.
 * @returns The requests, filled as they are sent.
 */
function writes(page: Page, environment: string): string[] {
  const sent: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() !== "GET" &&
      (path.includes("/connection") || path.startsWith("/api/v1/sign-in/step-up"))
    ) {
      sent.push(`${request.method()} ${path.replace(environment, "ENV")}`);
    }
  });
  return sent;
}

/**
 * Chooses the environment of the Add form in its picker.
 *
 * @param page - The page.
 * @param environment - The environment.
 */
async function chooseEnvironment(page: Page, environment: string): Promise<void> {
  await page.getByRole("button", { name: /^Environment: / }).click();
  await page.getByRole("option", { name: environment, exact: true }).click();
  await expect(page.getByRole("button", { name: `Environment: ${environment}` })).toBeVisible();
}

/**
 * The row of the list that names a connection.
 *
 * @param page - The page.
 * @param name - The connection.
 * @returns The row.
 */
function rowOf(page: Page, name: string) {
  return page.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) });
}

test("a data engineer adds a directory connection in dev, sees it never tested, tests it and sees it queued", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  test.setTimeout(120_000);
  const environment = uniqueName("dev");
  const name = uniqueName("lake");
  await createEnvironment(request, environment);
  const { page } = await invitedPerson(browser, request, async (person) => {
    await grantOn(request, person, "READ_ENVIRONMENT", environment);
    await grantOn(request, person, "MANAGE_CONNECTION", environment);
  });
  await skipTheWelcome(page);

  await page.goto("/connections");
  await expect(
    page
      .getByText(`No connections in ${environment} yet.`)
      .or(page.getByText("No connections yet.")),
  ).toBeVisible();
  expect(await violations(page), "the empty list").toEqual([]);
  await page.getByRole("link", { name: "Add connection" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add connection" })).toBeVisible();
  const kinds = page.getByRole("radiogroup", { name: "Kind" });
  await kinds.getByText("Directory", { exact: true }).click();
  await page.getByRole("textbox", { name: "Name" }).fill(name);
  await page.getByRole("textbox", { name: "root" }).fill("landing");
  await page.getByRole("textbox", { name: "path" }).fill("orders");
  expect(await violations(page), "the filled form").toEqual([]);
  // The passkey touch the server asks for is answered by the virtual authenticator.
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(new RegExp(`/connections/${environment}/${name}$`));
  await expect(page.getByRole("status").filter({ hasText: `Created ${name}.` })).toBeFocused();
  await expect(page.getByText("never tested")).toBeVisible();
  expect(await violations(page), "the page of the new connection").toEqual([]);

  // The list shows it never tested, with Test; Test queues it, and no worker takes it.
  await page.getByRole("link", { name: "Connections", exact: true }).first().click();
  await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
  const row = rowOf(page, name);
  await expect(row).toContainText("never tested");
  await row.getByRole("button", { name: `Test ${name}` }).click();
  await expect(row).toContainText("queued");
  await expect(row.getByRole("button", { name: /^Testing… / })).toBeDisabled();

  // A reload meets the open test: Test stays disabled, so it never meets the server's 409.
  const posts = writes(page, environment);
  await page.reload();
  await expect(rowOf(page, name)).toContainText("queued");
  await expect(rowOf(page, name).getByRole("button", { name: /^Testing… / })).toBeDisabled();
  expect(posts).toEqual([]);
  expect(await violations(page), "the list with a queued test").toEqual([]);
  await page.context().close();
});

test("an administrator adds a postgresql connection with a password, and the value appears nowhere on the page after", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  test.setTimeout(120_000);
  const environment = uniqueName("adm");
  const name = uniqueName("sales");
  await createEnvironment(request, environment);
  const { page } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const withPassword = carrying(page, PASSWORD);
  const sent = writes(page, environment);

  await page.goto("/connections/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add connection" })).toBeVisible();
  await chooseEnvironment(page, environment);
  await page.getByRole("textbox", { name: "Name" }).fill(name);
  // PostgreSQL is the first kind and the one chosen.
  await page.getByRole("textbox", { name: "host" }).fill("db.example.com");
  await page.getByRole("textbox", { name: "database" }).fill("sales");
  await page.getByRole("textbox", { name: "user" }).fill("reader");
  const password = page.getByLabel("password", { exact: true });
  await expect(password).toHaveAttribute("type", "password");
  await expect(password).toHaveAttribute("autocomplete", "new-password");
  await password.fill(PASSWORD);
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(new RegExp(`/connections/${environment}/${name}$`));
  await expect(page.getByRole("status").filter({ hasText: `Created ${name}.` })).toBeFocused();
  await expect(page.getByText("(its value is never shown)")).toBeVisible();
  // The touch of the sign-in is fresh, so the create is sent once; the value is in that request
  // and in no other.
  expect(sent).toEqual(["POST /api/v1/environments/ENV/connections"]);
  expect(withPassword).toEqual([`POST /api/v1/environments/${environment}/connections`]);

  // Everything a person or a script could find of the value in the browser.
  const traces = async (): Promise<string[]> =>
    page.evaluate((value) => {
      const stores = [window.localStorage, window.sessionStorage].flatMap((store) =>
        Object.entries(store).map(([key, item]) => `${key}=${String(item)}`),
      );
      return [document.documentElement.outerHTML, window.location.href, ...stores].filter((text) =>
        text.includes(value),
      );
    }, PASSWORD);
  expect(await traces(), "the page after the save").toEqual([]);

  // Not in the list, not in Edit, not after a reload, not in the server's answer.
  await page.reload();
  expect(await traces(), "after a reload").toEqual([]);
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
  for (const field of await page.locator('input[type="password"]').all()) {
    await expect(field).toHaveValue("");
  }
  expect(await traces(), "Edit").toEqual([]);
  const answer = await page.request.get(`/api/v1/environments/${environment}/connections/${name}`);
  expect(await answer.text()).not.toContain(PASSWORD);
  expect(await violations(page), "Edit").toEqual([]);
  await page.context().close();
});

test("a reader sees the list and the page without Add, Test, Edit or Delete", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  test.setTimeout(120_000);
  const environment = uniqueName("rdr");
  const name = uniqueName("lake");
  await createEnvironment(request, environment);
  await createConnection(request, environment, {
    name,
    kind: "directory",
    address: { root: "landing", path: "orders" },
    settings: {},
  });
  const { page } = await invitedPerson(browser, request, (person) =>
    grantOn(request, person, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(page);

  await page.goto("/connections");
  await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
  await expect(rowOf(page, name)).toContainText("never tested");
  await expect(page.getByRole("link", { name: "Add connection" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Test/ })).toHaveCount(0);
  expect(await violations(page), "a reader's list").toEqual([]);

  await page.getByRole("link", { name, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(page.getByText("never tested")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Test/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete…" })).toHaveCount(0);
  expect(await violations(page), "a reader's page").toEqual([]);

  // The addresses of Add and Edit say why there is nothing to do, with no form.
  await page.goto("/connections/new");
  await expect(
    page.getByText(
      "You may not add connections in any environment. Ask an administrator for MANAGE_CONNECTION.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Save" })).toHaveCount(0);
  await page.goto(`/connections/${environment}/${name}/edit`);
  await expect(page.getByText(`You may not edit connections in ${environment}.`)).toBeVisible();
  await expect(page.getByRole("button", { name: "Save" })).toHaveCount(0);
  expect(await violations(page), "Edit without the right").toEqual([]);
  await page.context().close();
});

test("a data engineer's host change asks for the password again", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  test.setTimeout(120_000);
  const environment = uniqueName("eng");
  const name = uniqueName("sales");
  await createEnvironment(request, environment);
  await createConnection(request, environment, {
    name,
    kind: "postgresql",
    address: { host: "db.example.com", port: 5432, database: "sales" },
    settings: { user: "reader", tls: "verify-full" },
    secret_values: { password: "the-old-password" },
  });
  const { page, name: person } = await invitedPerson(browser, request, async (person) => {
    await grantOn(request, person, "READ_ENVIRONMENT", environment);
    await grantOn(request, person, "MANAGE_CONNECTION", environment);
  });
  await skipTheWelcome(page);
  const sent = writes(page, environment);
  const bodies: string[] = [];
  page.on("request", (request_) => {
    if (request_.method() === "PATCH") {
      bodies.push(request_.postData() ?? "");
    }
  });

  await page.goto(`/connections/${environment}/${name}/edit`);
  // Before any change: the reason is up front.
  await expect(
    page.getByText(`You may not manage secrets in ${environment}.`, { exact: false }),
  ).toBeVisible();
  const keep = page.getByRole("radio", { name: new RegExp(`^Keep ${name}`, "u") });
  await expect(keep).toBeChecked();
  expect(await violations(page), "Edit with the note").toEqual([]);

  // A host change moves the secret: Keep cannot stay, and says why.
  await page.getByRole("textbox", { name: "host" }).fill("db.other.example.com");
  await expect(keep).toBeDisabled();
  await expect(
    page.getByText(/^This change could send .+ somewhere new; give its values again\.$/u),
  ).toBeVisible();
  const password = page.getByLabel("password", { exact: true });
  await expect(password).toBeVisible();
  // Save is refused here until the value is given; nothing is sent.
  await page.getByRole("button", { name: "Save" }).click();
  await expect(password).toBeFocused();
  expect(sent).toEqual([]);
  expect(await violations(page), "Edit with the password asked for").toEqual([]);

  await password.fill("the-new-password");
  // The touch of the sign-in has gone stale: the server asks for a new one, which the virtual
  // authenticator gives, and the change is sent again.
  ageThePasskeyTouch(person);
  // Enter in the field sends the form: the error of the first Save goes when the field is left,
  // which moves Save up a line, so a press on the button would miss it.
  await password.press("Enter");
  await expect(page.getByRole("status").filter({ hasText: `Saved ${name}.` })).toBeFocused();
  // The change carries the host and the values, nothing else; the touch is asked for once.
  const body = JSON.parse(bodies[0] ?? "null") as unknown;
  expect(body).toEqual({
    address: { host: "db.other.example.com" },
    secret_values: { password: "the-new-password" },
  });
  expect(sent).toEqual([
    "PATCH /api/v1/environments/ENV/connections/" + name,
    "POST /api/v1/sign-in/step-up/options",
    "POST /api/v1/sign-in/step-up",
    "PATCH /api/v1/environments/ENV/connections/" + name,
  ]);
  await expect(page.getByText("db.other.example.com")).toBeVisible();
  await expect(page.getByText(/tested before the last change|never tested/u)).toBeVisible();
  await page.context().close();
});

test("deleting asks first and the list no longer shows it", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  test.setTimeout(120_000);
  const environment = uniqueName("del");
  const name = uniqueName("lake");
  await createEnvironment(request, environment);
  await createConnection(request, environment, {
    name,
    kind: "directory",
    address: { root: "landing", path: "orders" },
    settings: {},
  });
  const { page } = await invitedPerson(browser, request, async (person) => {
    await grantOn(request, person, "READ_ENVIRONMENT", environment);
    await grantOn(request, person, "MANAGE_CONNECTION", environment);
  });
  await skipTheWelcome(page);
  const sent = writes(page, environment);

  await page.goto("/connections");
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await page.getByRole("button", { name: "Delete…" }).click();
  const dialog = page.getByRole("dialog", {
    name: `Delete the connection ${name} in ${environment}?`,
  });
  await expect(dialog.getByText("It has no secret.")).toBeVisible();
  expect(await violations(page), "the Delete dialog").toEqual([]);

  // Cancel asks nothing of the server, and the connection stays.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(sent).toEqual([]);
  expect(await connectionStatus(request, environment, name)).toBe(200);

  await page.getByRole("button", { name: "Delete…" }).click();
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/\/connections$/);
  await expect(page.getByRole("status").filter({ hasText: `Deleted ${name}.` })).toBeFocused();
  await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  expect(sent).toEqual([`DELETE /api/v1/environments/ENV/connections/${name}`]);
  expect(await connectionStatus(request, environment, name)).toBe(404);
  expect(await violations(page), "the list after a delete").toEqual([]);
  await page.context().close();
});
