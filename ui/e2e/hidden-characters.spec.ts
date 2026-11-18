import { expect, type Page, test } from "@playwright/test";

import { apiTypes, typeOf } from "./api-types";
import { ARTIFACTS_STUBBED, stubArtifacts } from "./artifacts";
import { ROUTES, stubConnect } from "./connect";
import {
  apiPaths,
  expectMarked,
  expectNoRaw,
  poison,
  poisoned,
  rawOverrides,
  READ_AS,
  readKey,
  RLO,
  STRING_CODES,
  takeReads,
} from "./hidden";
import {
  ADHOC_MESSAGE,
  ADHOC_REPOSITORY,
  DEPLOY_FAILURE,
  DEV_JOB,
  FAILED_CHECK,
  FAILED_ID,
  JOBS_STUBBED,
  MISSING_ID,
  MISSING_SENTENCE,
  OLDER_ONLY_JOB,
  SECRET,
  SELECTION_PATTERNS,
  stubJobs,
  TIMED_OUT,
  TIMEOUT_CAUSE,
} from "./jobs";
import { MANAGE_STUBBED, stubGitOwned, stubManagement } from "./manage";
import { PLACES_STUBBED, stubPlaces } from "./places";
import { ADMIN_SESSION, signInAsAdmin, STUBBED } from "./session";

// Every stubbed answer carries a right-to-left override in each text field (e2e/hidden.ts); each
// step names stubbed values its screen must show with the mark U+202E, and no screen may show the
// override itself. A failure names the element that shows it raw.

/**
 * A name as the poisoned answers hold it, written for an address: a link the page builds from
 * them carries the override too, and so may a link someone sends.
 *
 * @param name - The name as the stubs write it.
 * @returns The name with the override, encoded.
 */
function n(name: string): string {
  return `${name}%E2%80%AE`;
}

const TABLE = `/catalog/${n("prod")}/${n("gold.sales")}/${n("fact_sales")}`;
const ARTIFACT = `/catalog/${n("prod")}/${n("ml")}/${n("model")}`;

/** The paths `stubAccounts` answers. */
const ACCOUNTS = [
  "/api/v1/invites",
  "/api/v1/recovery-links",
  "/api/v1/principals/admin/passkeys",
  "/api/v1/principals/admin/sessions",
];

/** What `apiTypes()` finds today: each type with a literal-union field, and those fields. */
const LITERAL_CODES: Record<string, string> = {
  AllRunsFilter: "status trigger",
  ArtifactKeptOut: "reason",
  ArtifactOut: "actions kind",
  ArtifactVersionDeletedOut: "actions",
  ArtifactVersionOut: "actions",
  AttentionItemOut: "kind",
  CheckResultOut: "outcome",
  ColumnOut: "description_source label_source",
  DeployOut: "actions kind status",
  DeployTriggerOut: "kind",
  DroppedTableOut: "actions",
  ExternalTableOut: "format",
  GetStartedOut: "audience",
  GetStartedStepOut: "id",
  GrantOut: "kind role",
  GraphEdgeOut: "kind",
  GraphNodeOut: "kind",
  HealthBandOut: "basis weekday",
  HealthObservedOut: "kind reason source",
  HealthSummaryOut: "state",
  InstanceStatusOut: "state",
  JobOut: "actions last_run_status",
  JobRunFilter: "status",
  LastMaintenanceOut: "status",
  LinkDescriptionOut: "kind state",
  ListTablesQuery: "order sort",
  NotifyOut: "format",
  OnDemandTriggerOut: "kind",
  OrphanOut: "actions kind",
  OwnerOut: "kind",
  QueryListItemOut: "origin status",
  QueryRunOut: "origin status",
  QueryStatusOut: "status",
  RecentOut: "kind",
  RepositoryCreate: "kind",
  RepositoryProbeOut: "kind outcome state",
  RunOut: "actions kind status trigger",
  SavedQueryIn: "visibility",
  SavedQueryOut: "actions visibility",
  ScheduleTriggerOut: "kind",
  ScopeOut: "kind",
  SubjectPrincipalOut: "principal_type",
  TableDefinitionFileOut: "reason",
  TableListItemOut: "kind state",
  TableMatchOut: "kind matched",
  TableOut: "actions description_source kind label_source state",
  TableRetentionOut: "kept source",
  "TableTreeOut.environments": "actions",
  "TableTreeOut.environments.namespaces": "actions",
  TaskRunOut: "kind status",
  ViaOut: "kind role",
};

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "the check reads the DOM, the same in every browser");
});

/** The type each object of this file's own answers to `poison` is read as, as READ_AS pins those of the stubs. */
const SYNTHETIC_READ_AS: Readonly<Record<string, string>> = {
  '["action","allowed","reason","via"]': "PermissionOut",
  '["capped","items","next_cursor"]': "TableListPageOut",
  '["detail","type"]': "ErrorBody",
  '["distance","id","kind","name"]': "GraphNodeOut",
  '["edges","error","nodes","permissions"]': "GraphOut",
  '["environment","health","kind","last_write_at","name","namespace","owner","state","uuid"]':
    "TableListItemOut",
  '["item","others","state","text"]': "HealthSummaryOut",
  '["kind","name"]': "OwnerOut",
  '["kind","source","target"]': "GraphEdgeOut",
};

/** The objects the stubs served in this worker, by {@link readKey}. */
const served = new Set<string>();

/** The walks of the screens, by title; filled as they are declared. */
const WALKS: string[] = [];

/** The walks that passed in this worker. */
const walked = new Set<string>();

/**
 * The objects read as another type than the pins say, or not pinned.
 *
 * @param reads - The objects read ({@link takeReads}).
 * @param pins - The pins.
 * @returns One line for each: its keys, the type it was read as, and the one pinned.
 */
function misread(
  reads: ReadonlyMap<string, string>,
  pins: Readonly<Record<string, string>>,
): string[] {
  return [...reads]
    .filter(([keys, name]) => pins[keys] !== name)
    .map(([keys, name]) => `${JSON.stringify(keys)}: "${name}" (pinned: ${pins[keys] ?? "none"})`)
    .sort();
}

// A fixture object read as another type would leave that type's codes alone, a free-text field
// among them: each object is read as the type READ_AS pins.
test.afterEach(() => {
  const testInfo = test.info();
  const reads = takeReads();
  reads.forEach((_, keys) => served.add(keys));
  if (testInfo.status === "passed") {
    walked.add(testInfo.title);
  }
  expect(misread(reads, READ_AS), "each stubbed object read as the type READ_AS pins").toEqual([]);
});

/**
 * Declares a walk of screens: it fails on an error the page throws and on a script or style of
 * the build that did not load, which would otherwise show only as a value not found.
 *
 * @param title - The test's title.
 * @param body - The walk.
 */
function walk(title: string, body: (fixtures: { page: Page }) => Promise<void>): void {
  WALKS.push(title);
  test(title, async ({ page }) => {
    const broken: string[] = [];
    page.on("pageerror", (error) => broken.push(`page error: ${error.message}`));
    page.on("requestfailed", (request) => {
      if (new URL(request.url()).pathname.startsWith("/assets/")) {
        broken.push(`${request.url()}: ${request.failure()?.errorText ?? "failed"}`);
      }
    });
    try {
      await body({ page });
    } finally {
      expect.soft(broken, "the page threw or missed a file of the build").toEqual([]);
    }
  });
}

/**
 * Requires that every API request went to a stubbed path: any other reached `vite preview` and
 * got no poisoned answer.
 *
 * @param paths - The paths sent ({@link apiPaths}).
 * @param stubbed - The stubbed paths.
 */
function expectOnlyStubbed(paths: readonly string[], stubbed: readonly string[]): void {
  expect(paths.length, "the screens asked the API").toBeGreaterThan(0);
  expect(paths.filter((path) => !stubbed.includes(path))).toEqual([]);
}

/**
 * Opens an address and checks it.
 *
 * @param page - The page.
 * @param address - The address.
 * @param values - Stubbed values the screen shows.
 */
async function visit(
  page: Page,
  address: string,
  values: readonly [string, ...string[]],
): Promise<void> {
  await page.goto(address);
  await expectMarked(page, address, values);
}

/**
 * Opens the Actions menu and each of its items in turn, checking each dialog.
 *
 * @param page - The page.
 * @param actions - The name of the Actions button.
 * @param values - Stubbed values the page and every dialog show.
 * @param own - For a dialog that reads its own answer, by its item: a value from that answer.
 */
async function eachAction(
  page: Page,
  actions: RegExp,
  values: readonly [string, ...string[]],
  own: Readonly<Record<string, readonly [string, ...string[]]>> = {},
): Promise<void> {
  const button = page.getByRole("button", { name: actions }).first();
  await button.click();
  await expect(page.getByRole("menu")).toBeVisible();
  // The menu holds the interface's own words; a disabled item's reason is checked by the raw scan.
  await expectNoRaw(page, `${actions.source} menu`);
  const items = await page.getByRole("menuitem").allTextContents();
  await page.keyboard.press("Escape");
  for (const item of items) {
    await button.click();
    const entry = page.getByRole("menuitem", { name: item, exact: true });
    if (await entry.isDisabled()) {
      await page.keyboard.press("Escape");
      continue;
    }
    await entry.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expectMarked(page, `the ${item} dialog`, own[item] ?? values);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
}

test("the check finds a hidden character shown as itself, and leaves a field's value alone", async ({
  page,
}) => {
  await page.setContent(
    `<title>t${RLO}</title><p>a<span>b${RLO}</span></p><button aria-label="c${RLO}">c</button>` +
      `<img alt="d${RLO}" src="data:,"><svg><title>e${RLO}</title></svg>` +
      `<select><option label="i${RLO}">i</option></select><input type="submit" value="j${RLO}">` +
      `<textarea>f${RLO}</textarea><input value="g${RLO}"><p>h<span class="char-mark">U+202E</span></p>`,
  );

  expect(await rawOverrides(page)).toEqual([
    "document title: t⟨RLO⟩",
    "text in body > p > span: b⟨RLO⟩",
    "text in body > svg > title: e⟨RLO⟩",
    "aria-label of html > body > button: c⟨RLO⟩",
    "alt of html > body > img: d⟨RLO⟩",
    "label of body > select > option: i⟨RLO⟩",
    "value of html > body > input: j⟨RLO⟩",
  ]);
});

test("the answers carry the override in every text but each type's codes, ids and times", () => {
  expect(
    poison({
      // A table's row of the list: kind and state are literal unions of TableListItemOut.
      items: [
        {
          environment: "prod",
          namespace: "gold",
          name: "fact_sales",
          uuid: "u-1",
          kind: "TABLE",
          state: "failing",
          owner: { name: "kim", kind: "USER" },
          health: { state: "failing", item: "check_failed", text: "Check failed", others: 0 },
          last_write_at: "2026-11-09T10:00:00Z",
        },
      ],
      next_cursor: "c-2",
      capped: false,
    }),
  ).toEqual({
    items: [
      {
        environment: `prod${RLO}`,
        namespace: `gold${RLO}`,
        name: `fact_sales${RLO}`,
        uuid: `u-1${RLO}`,
        kind: "TABLE",
        state: "failing",
        owner: { name: `kim${RLO}`, kind: "USER" },
        health: { state: "failing", item: "check_failed", text: `Check failed${RLO}`, others: 0 },
        last_write_at: "2026-11-09T10:00:00Z",
      },
    ],
    next_cursor: `c-2${RLO}`,
    capped: false,
  });
  // A name that is a code in one type is text in another: PermissionOut's reason is text, a
  // lineage node's kind a code. An error's type, ids and the ends of an edge stay as they are.
  expect(
    poison({
      permissions: [{ action: "drop", allowed: false, via: [], reason: "Not the owner." }],
      nodes: [{ id: "table:a", kind: "table", name: "gold.a", distance: 0 }],
      edges: [{ kind: "reads", source: "table:a", target: "table:a" }],
      error: { type: "StepUpRequired", detail: "Touch your passkey." },
    }),
  ).toEqual({
    permissions: [
      { action: `drop${RLO}`, allowed: false, via: [], reason: `Not the owner.${RLO}` },
    ],
    nodes: [{ id: "table:a", kind: "table", name: `gold.a${RLO}`, distance: 0 }],
    edges: [{ kind: "reads", source: "table:a", target: "table:a" }],
    error: { type: "StepUpRequired", detail: `Touch your passkey.${RLO}` },
  });
  expect(misread(takeReads(), SYNTHETIC_READ_AS)).toEqual([]);
});

test("each object of the answers is read as the type READ_AS pins", () => {
  const types = apiTypes();
  const picks = (pins: Readonly<Record<string, string>>): Record<string, string> =>
    Object.fromEntries(
      Object.keys(pins).map((keys) => {
        const parsed = JSON.parse(keys) as string[];
        expect(readKey(parsed), "a key as readKey writes it").toBe(keys);
        return [keys, typeOf(types, parsed)?.name ?? "-"];
      }),
    );

  // A change here changes which codes an object's fields are read as: check its free text.
  expect(picks(READ_AS)).toEqual(READ_AS);
  expect(picks(SYNTHETIC_READ_AS)).toEqual(SYNTHETIC_READ_AS);
});

test("the codes of each answer type are the ones src/api declares", () => {
  const literal = Object.fromEntries(
    [...apiTypes().values()]
      .filter((type) => type.literal.size > 0)
      .map((type) => [type.name, [...type.literal].sort().join(" ")]),
  );

  // A change here changes what the check leaves alone: read the type before accepting it.
  expect(literal).toEqual(LITERAL_CODES);
  for (const [type, fields] of Object.entries(STRING_CODES)) {
    for (const field of fields) {
      expect(apiTypes().get(type)?.fields.has(field), `${type}.${field}`).toBe(true);
    }
  }
});

const AT = "2026-11-09T10:00:00Z";

/**
 * Answers what Settings' Access and Sign-in places and one's account read beyond `session.ts`: an open invite, an
 * open recovery link, a passkey, a session, and a sign-in notice about another person.
 *
 * @param page - The page to stub, poisoned.
 */
async function stubAccounts(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/notices", (route) =>
    route.fulfill({
      json: [{ id: "n-1", kind: "recovery_used", subject: "kim.schulz", created_at: AT }],
    }),
  );
  await page.route("**/api/v1/invites", (route) =>
    route.fulfill({
      json: [{ id: "i-1", name: "sam", display_name: "Sam Kay", created_at: AT, expires_at: AT }],
    }),
  );
  await page.route("**/api/v1/recovery-links", (route) =>
    route.fulfill({ json: [{ id: "r-1", name: "kim.schulz", created_at: AT, expires_at: AT }] }),
  );
  await page.route("**/api/v1/principals/admin/passkeys", (route) =>
    route.fulfill({
      json: [
        {
          id: "p-1",
          name: "Work laptop",
          created_at: AT,
          last_used_at: AT,
          backup_eligible: true,
          disabled_reason: null,
          usable_here: true,
        },
      ],
    }),
  );
  await page.route("**/api/v1/principals/admin/sessions", (route) =>
    route.fulfill({
      json: [
        {
          id: "s-1",
          via: "passkey",
          created_at: AT,
          last_seen_at: AT,
          absolute_expires_at: AT,
          user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/155.0.0.0 Safari/537.36",
          browser: "Chrome 155 on Linux",
          peer: "127.0.0.1",
          current: true,
        },
      ],
    }),
  );
}

walk("Home, the Catalog and People mark hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await signInAsAdmin(poisoned(page));
  await visit(page, "/", ["daily_sales"]);
  await visit(page, "/catalog", ["fact_sales"]);
  await visit(page, `/catalog?env=${n("prod")}`, ["fact_sales"]);
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold.sales")}`, ["fact_sales"]);
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold")}`, ["station_ranking"]);
  // A column hit and a translated description: the snippet and its language.
  await visit(page, "/catalog?q=store", ["store_id", "ranked by the trips it served"]);
  await expect(page.locator(".match-line > span", { hasText: /^deU\+202E$/ })).toHaveCount(1);
  await visit(page, `/catalog?env=${n("staging")}`, ["staging"]);
  await visit(page, "/settings/access/people", ["admin"]);
  expectOnlyStubbed(paths, STUBBED);
});

walk(
  "every tab of a table's page, its editors and its dialogs mark hidden characters",
  async ({ page }) => {
    const paths = apiPaths(page);
    await signInAsAdmin(poisoned(page));
    await visit(page, TABLE, ["Every sale in every store, one row per sale."]);
    await visit(page, `${TABLE}/columns`, ["store_id"]);
    await visit(page, `${TABLE}/lineage`, ["gold.sales.daily_sales"]);
    await visit(page, `${TABLE}/references`, ["gold.dim_store"]);
    await visit(page, `${TABLE}/health`, ["unique_sale_id"]);
    await visit(page, `${TABLE}/access`, ["kim.schulz"]);
    await visit(page, `${TABLE}/preview`, ["customer_email"]);
    await page.getByRole("gridcell").nth(2).click();
    await expectMarked(page, "the Value panel", ["and more "]);

    await visit(page, `${TABLE}/lineage`, ["gold.sales.daily_sales"]);
    await page.getByRole("button", { name: "Full screen" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expectMarked(page, "the full-screen lineage", ["gold.sales.daily_sales"]);
    await page.keyboard.press("Escape");

    await visit(page, TABLE, ["models/gold/fact_sales.sql"]);
    await page.getByRole("button", { name: /fact_sales\.sql/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expectMarked(page, "the file of Defined in", ["models/gold/fact_sales.sql"]);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await expectMarked(page, "the editor of the table's texts", ["fact_sales"]);

    await visit(page, `${TABLE}/columns`, ["store_id"]);
    await page.getByRole("button", { name: /^Edit store_id/ }).click();
    await expectMarked(page, "the editor of a column's texts", ["store_id"]);
    expectOnlyStubbed(paths, STUBBED);
  },
);

walk(
  "a table's actions, the catalog's views and a git-owned table mark hidden characters",
  async ({ page }) => {
    const paths = apiPaths(page);
    await stubManagement(poisoned(page));
    await visit(page, `/catalog?env=${n("prod")}&view=dropped`, ["fact_returns"]);
    await visit(page, `/catalog?env=${n("prod")}&view=expiring`, ["late_arrival"]);
    await visit(page, `/catalog?env=${n("prod")}&view=orphans`, ["clean"]);
    await visit(page, TABLE, ["fact_sales"]);
    // Retention reads the table's rule; its title alone comes from the address.
    await eachAction(page, /^Actions$/, ["fact_sales"], { "Retention…": ["ana"] });
    await stubGitOwned(poisoned(page));
    await visit(page, TABLE, ["fact_sales"]);
    await eachAction(page, /^Actions$/, ["fact_sales"]);
    expectOnlyStubbed(paths, MANAGE_STUBBED);
  },
);

walk("the place menu and its dialogs mark hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubPlaces(poisoned(page));
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("bronze")}`, ["bronze"]);
  await eachAction(page, /^Actions for bronze/, ["bronze"]);
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold")}`, ["gold"]);
  await eachAction(page, /^Actions for gold/, ["gold"]);
  await visit(page, `/catalog?env=${n("dev")}&ns=${n("gold")}`, ["gold"]);
  await eachAction(page, /^Actions for gold/, ["gold"]);
  expectOnlyStubbed(paths, PLACES_STUBBED);
});

walk("an artifact's page marks hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubArtifacts(poisoned(page));
  await visit(page, ARTIFACT, ["ana.nowak"]);
  // The stub's lineage holds the artifact alone, which the tab does not name ("Nothing upstream
  // or downstream"): the header's name is all the step can require.
  await visit(page, `${ARTIFACT}/lineage`, ["model"]);
  await visit(page, `${ARTIFACT}/access`, ["kim.schulz"]);
  await visit(page, `${ARTIFACT}/versions`, ["model"]);
  await page.getByRole("button", { name: /^Files of version 2/ }).click();
  await expectMarked(page, "the files of version 2", ["models/v2/model.pkl"]);
  await visit(page, ARTIFACT, ["model"]);
  await eachAction(page, /^Actions$/, ["model"]);
  expectOnlyStubbed(paths, ARTIFACTS_STUBBED);
});

walk(
  "Settings' Access and Sign-in places, one's account and a sign-in notice mark hidden characters",
  async ({ page }) => {
    const paths = apiPaths(page);
    await signInAsAdmin(poisoned(page));
    await stubAccounts(poisoned(page));
    await visit(page, "/", ["kim.schulz"]);
    await visit(page, "/settings/sign-in/invites", ["sam"]);
    await visit(page, "/settings/sign-in/recovery-links", ["kim.schulz"]);
    await visit(page, `/settings/access/people/${n("admin")}`, ["Work laptop"]);
    await visit(page, "/account", ["Work laptop"]);
    expectOnlyStubbed(paths, [...STUBBED, ...ACCOUNTS]);
  },
);

walk("an invite link marks hidden characters in the name it signs in as", async ({ page }) => {
  const paths = apiPaths(page);
  const stub = poisoned(page);
  await stub.route("**/api/v1/sign-in/session", (route) =>
    route.fulfill({ status: 401, json: { detail: "Sign in.", type: "AuthenticationError" } }),
  );
  await stub.route("**/api/v1/sign-in/links/describe", (route) =>
    route.fulfill({
      json: { kind: "invite", name: ADMIN_SESSION.name, display_name: "Ada", state: "open" },
    }),
  );
  await visit(page, "/sign-in#l=pub-1&s=sec-1", ["Ada"]);
  expectOnlyStubbed(paths, ["/api/v1/sign-in/session", "/api/v1/sign-in/links/describe"]);
});

walk("Connect a repository marks hidden characters in every step", async ({ page }) => {
  const paths = apiPaths(page);
  await signInAsAdmin(poisoned(page));
  await stubConnect(poisoned(page));
  await page.goto("/settings/repositories/new");
  const url = page.getByRole("textbox", { name: "Repository URL" });
  await url.fill("https://github.com/acme/analytics");
  await url.blur();
  await page.getByRole("button", { name: "Use a token instead" }).click();
  await page.getByRole("button", { name: "Fetch the host's keys" }).click();
  // The host's preferred key type carries the override too, so every key shows at once.
  await expect(page.getByRole("checkbox", { name: /^This matches: ssh-rsa/ })).toBeVisible();
  await expectMarked(page, "the host's keys", ["ssh-ed25519", "ssh-rsa"]);
  await page.getByRole("checkbox", { name: /^This matches: ssh-ed25519/ }).check();
  await page.getByRole("button", { name: "Create the repository and its keys" }).click();
  await expect(page.getByRole("button", { name: /^Test access in dev/ })).toBeVisible();
  await expectMarked(page, "the keys", [
    "SHA256:dev7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe",
    "The key of prod was made earlier; it stays as it is.",
  ]);
  await page.getByRole("button", { name: /^Test access in dev/ }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expectMarked(page, "a refused test", ["then test again."]);
  expectOnlyStubbed(paths, [...STUBBED, ...ROUTES]);
});

walk("the results of the catalog's actions mark hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubManagement(poisoned(page));
  await visit(page, TABLE, ["fact_sales"]);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Maintain now…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Maintain now" }).click();
  await expect(page.getByRole("dialog").getByText(/is queued/)).toBeVisible();
  await expectMarked(page, "the queued maintenance", ["fact_sales"]);
  await page.keyboard.press("Escape");

  await visit(page, `/catalog?env=${n("prod")}&view=dropped`, ["fact_returns"]);
  await page.getByRole("button", { name: /^Undrop gold\.sales.*fact_returns/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "Restored" })).toBeVisible();
  await expectMarked(page, "an Undrop", ["fact_returns"]);
  expectOnlyStubbed(paths, MANAGE_STUBBED);
});

walk("the notes and refusals of the place menu mark hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubPlaces(poisoned(page));
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("bronze")}`, ["bronze"]);
  await page.getByRole("button", { name: /^Actions for bronze/ }).click();
  await page.getByRole("menuitem", { name: "New namespace…" }).click();
  const create = page.getByRole("dialog");
  const path = create.getByRole("textbox", { name: "Path" });
  await path.press("End");
  await path.pressSequentially("landing");
  const button = create.getByRole("button", { name: "Create" });
  await expect(button).not.toHaveAttribute("data-pending");
  await button.click();
  await expect(page.locator(".catalog-result", { hasText: /^Created namespace/ })).toBeVisible();
  await expectMarked(page, "the note after a create", ["landing"]);

  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold.sales")}`, ["gold.sales"]);
  await page.getByRole("button", { name: /^Actions for gold\.sales/ }).click();
  await page.getByRole("menuitem", { name: "New external table…" }).click();
  const refused = page.getByRole("dialog");
  await refused.getByRole("textbox", { name: "Name" }).fill("orders");
  await refused.getByRole("textbox", { name: "Location" }).fill("raw/none/*.csv");
  const register = refused.getByRole("button", { name: "Register" });
  await expect(register).not.toHaveAttribute("data-pending");
  await register.click();
  await expect(refused.getByRole("alert")).toBeVisible();
  await expectMarked(page, "a refused registration", ["No file matches 'raw/none/*.csv'."]);
  expectOnlyStubbed(paths, PLACES_STUBBED);
});

walk("the job, run and task pages mark hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubJobs(poisoned(page));
  const env = n("prod");
  await visit(page, `/jobs/${env}/job/${n("analytics")}/${n("daily_sales")}`, [
    "daily_sales",
    "analytics",
    SECRET,
    "Waits for silver.orders to finish.",
  ]);
  await page.getByRole("button", { name: "Pause schedule" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the Pause dialog", ["daily_sales"]);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the Cancel dialog", ["daily_sales"]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();

  // The failed check's name is the label of its Show rows button.
  await visit(page, `/jobs/${env}/runs/${FAILED_ID}`, [
    TIMED_OUT,
    TIMEOUT_CAUSE,
    "daily_sales",
    FAILED_CHECK,
  ]);
  await expect(page.getByRole("button", { name: /^Show rows of .*U\+202E$/ })).toBeVisible();
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("button", { name: /^Show rows of / }) })
    .getByRole("button", { name: "Show source" })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the Show source dialog", ["models/gold/fact_sales.sql"]);
  await page.keyboard.press("Escape");

  await page.goto(`/jobs/${env}/runs/${FAILED_ID}/tasks/${n(TIMED_OUT)}`);
  await expect(page.getByText("The log is complete.")).toBeVisible({ timeout: 10_000 });
  await expectMarked(page, "a task with its log", [TIMED_OUT, "w-7f3a", TIMEOUT_CAUSE]);
  // Each piece of the log ends in the override, so the log shows three marks.
  await expect(page.getByRole("region", { name: /^Log of / }).locator(".char-mark")).toHaveCount(3);

  // A run that is not there: the server's sentence.
  await visit(page, `/jobs/${env}/runs/${MISSING_ID}`, [MISSING_SENTENCE]);
  expectOnlyStubbed(paths, JOBS_STUBBED);
});

walk("the Jobs list marks hidden characters", async ({ page }) => {
  const paths = apiPaths(page);
  await stubJobs(poisoned(page));
  // Running now, Failed (a task's cause and a run's own message) and All jobs.
  await visit(page, "/jobs", [
    "daily_sales",
    "analytics",
    DEV_JOB,
    TIMEOUT_CAUSE,
    ADHOC_MESSAGE,
    ADHOC_REPOSITORY,
  ]);
  await expect(page.getByRole("button", { name: /^Cancel the run of daily_sales/ })).toBeVisible();

  await page.getByRole("button", { name: /^Environment: / }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await expectNoRaw(page, "the open filter");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("listbox")).toBeHidden();

  await page.getByRole("button", { name: /^Cancel the run of daily_sales/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the Cancel dialog of a row", ["daily_sales"]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();

  await page.getByRole("button", { name: /^Pause the schedule of daily_sales/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the Pause dialog of a row", ["daily_sales"]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();

  // The search narrows what was read; the rows it keeps still show their marks.
  await page.getByRole("searchbox", { name: "Search jobs" }).fill(DEV_JOB);
  await expect(page.getByRole("link", { name: /^daily_sales/ })).toHaveCount(0);
  await expectMarked(page, "a search", [DEV_JOB]);
  expectOnlyStubbed(paths, JOBS_STUBBED);
});

walk(
  "the Runs and Deploys tabs and the rollback dialog mark hidden characters",
  async ({ page }) => {
    const paths = apiPaths(page);
    await stubJobs(poisoned(page));
    // Every run: the jobs' names, a run of no job by its repository; then the runs that ended,
    // with a selection's patterns.
    await visit(page, "/jobs/runs", ["daily_sales", DEV_JOB, "analytics"]);
    await page.getByRole("button", { name: "Show more" }).click();
    await expect(page.getByText(/^selection /)).toBeVisible();
    await expectMarked(page, "the runs that ended", [ADHOC_REPOSITORY, ...SELECTION_PATTERNS]);

    // One environment's deploys: the repository and who asked. A failed deploy's message is cut
    // to its first line, which here holds no override.
    const env = n("prod");
    await visit(page, `/jobs/deploys?environment=${env}`, ["analytics", "admin", "ana.nowak"]);
    await expect(page.getByText(DEPLOY_FAILURE, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /Repository/ }).click();
    await expect(page.getByRole("listbox")).toBeVisible();
    await expectNoRaw(page, "the open Repository menu");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("listbox")).toBeHidden();

    await page
      .getByRole("button", { name: /^Roll back to this… \(analytics/ })
      .first()
      .click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("button", { name: "Roll back" })).toBeVisible();
    await expectMarked(page, "the rollback dialog", ["analytics", "daily_sales", OLDER_ONLY_JOB]);
    await dialog.getByRole("button", { name: "Roll back" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(/^Rolled back /)).toBeVisible({ timeout: 10_000 });
    await expectMarked(page, "the note after a rollback", ["analytics"]);
    expectOnlyStubbed(paths, JOBS_STUBBED);
  },
);

// Last, so that the walks before it have run in this worker: an entry no stub serves is stale.
test("every object READ_AS pins is one a stub serves", () => {
  test.skip(
    WALKS.some((title) => !walked.has(title)),
    "needs every walk to have passed in this worker",
  );
  expect(Object.keys(READ_AS).filter((keys) => !served.has(keys))).toEqual([]);
});
