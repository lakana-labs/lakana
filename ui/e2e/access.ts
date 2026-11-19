/**
 * What the server answers Access's places with in the smoke tests: groups, members, grants, the
 * access files' status, and the administrator's own grants and permissions, kept in memory so a
 * test's writes show when the list is read again.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

/** A name that is long enough to make a table wider than a phone. */
export const LONG_NAME = `maria_${"x".repeat(294)}`;

/** The groups. */
const GROUPS = ["analysts", "ops"];

/** The paths this file's stubs answer, beside those of `session.ts`. */
export const ACCESS_STUBBED = [
  "/api/v1/access/status",
  "/api/v1/groups",
  ...GROUPS.flatMap((group) => [
    `/api/v1/groups/${group}`,
    `/api/v1/groups/${group}/members`,
    ...["ben", "maria", "ci-bot", LONG_NAME].map(
      (member) => `/api/v1/groups/${group}/members/${member}`,
    ),
  ]),
  "/api/v1/grants/g-1",
  "/api/v1/grants/g-2",
  "/api/v1/grants/g-3",
  "/api/v1/grants/g-4",
  "/api/v1/principals/admin/grants",
  "/api/v1/principals/admin/permissions",
  "/api/v1/principals/maria",
  "/api/v1/principals/maria/grants",
  "/api/v1/principals/maria/passkeys",
  "/api/v1/principals/maria/sessions",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** Options of {@link stubAccess}. */
export interface AccessStubOptions {
  /** The scopes an access file manages: `instance`, environments' names. */
  readonly managed?: readonly string[];
  /** Whether a member and a grant carry a 300-character name. */
  readonly longNames?: boolean;
}

const AT = "2026-11-10T08:00:00Z";

/** What a grant gives. */
interface What {
  /** The role, or null. */
  readonly role: string | null;
  /** The action, or null. */
  readonly action: string | null;
}

/** What a grant is on. */
interface Where {
  /** The level. */
  readonly kind: string;
  /** The environment, or null. */
  readonly environment: string | null;
  /** The dotted namespace, if any. */
  readonly namespace?: string | null;
}

/**
 * A grant as the Grants list answers it.
 *
 * @param id - Its id.
 * @param who - The group, or the person.
 * @param what - Its role and action.
 * @param where - What it is on.
 * @returns The grant.
 */
function grant(
  id: string,
  who: { group: string } | { person: string },
  what: What,
  where: Where,
): Record<string, unknown> {
  const group = "group" in who ? who.group : null;
  const person = "person" in who ? who.person : null;
  return {
    id,
    principal_id: person === null ? null : `${person}-id`,
    group_id: group === null ? null : `${group}-id`,
    principal:
      person === null ? null : { id: `${person}-id`, name: person, principal_type: "user" },
    group,
    ...what,
    kind: where.kind,
    environment: where.environment,
    namespace: where.namespace ?? null,
    table: null,
    granted_by: "admin-id",
    granted_at: AT,
    note: null,
  };
}

/**
 * Answers JSON.
 *
 * @param route - The route.
 * @param json - The body.
 * @param status - The status.
 * @returns When answered.
 */
function answer(route: Route, json: unknown, status = 200): Promise<void> {
  return route.fulfill({ status, json });
}

/**
 * Answers Access's routes: the groups `analysts` (maria, ci-bot) and `ops`, four grants across
 * the instance, `dev` and `prod`, the environments `dev` and `prod`, and the administrator's own
 * grants and permissions; writes change what the next read answers. Register it after
 * `signInAsAdmin`, whose answers for the environments and the grants it replaces.
 *
 * @param page - The page.
 * @param options - The managed scopes and whether names are long.
 */
export async function stubAccess(page: Page, options: AccessStubOptions = {}): Promise<void> {
  const managed = options.managed ?? [];
  const member = options.longNames === true ? LONG_NAME : "maria";
  const members = new Map<string, { name: string; type: string }[]>([
    [
      "analysts",
      [
        { name: "ci-bot", type: "SERVICE" },
        { name: member, type: "USER" },
      ],
    ],
    ["ops", []],
  ]);
  const groups = [...GROUPS];
  let grants = [
    grant(
      "g-1",
      { group: "analysts" },
      { role: "ANALYST", action: null },
      {
        kind: "ENVIRONMENT",
        environment: "dev",
      },
    ),
    grant(
      "g-2",
      { person: member },
      { role: "VIEWER", action: null },
      {
        kind: "NAMESPACE",
        environment: "prod",
        namespace: "gold.sales",
      },
    ),
    grant(
      "g-3",
      { person: "admin" },
      { role: "ADMIN", action: null },
      {
        kind: "INSTANCE",
        environment: null,
      },
    ),
  ];
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    (route) =>
      answer(
        route,
        ["dev", "prod"].map((name) => ({
          name,
          storage_root: `file:///srv/lakana/${name}`,
          storage_secret_ref: null,
          is_protected: name === "prod",
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      ),
  );
  await page.route("**/api/v1/access/status", (route) =>
    answer(route, {
      scopes: managed.map((scope) => ({
        scope,
        file_name: "access.yaml",
        file_sha256: "ab12cd",
        source_commit: null,
        applied_by: "admin",
        applied_at: AT,
      })),
      overrides: [],
    }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/groups",
    (route) => {
      const request = route.request();
      if (request.method() === "POST") {
        const { name } = request.postDataJSON() as { name: string };
        groups.unshift(name);
        members.set(name, []);
        return answer(route, { name }, 201);
      }
      const q = new URL(request.url()).searchParams.get("q") ?? "";
      return answer(
        route,
        groups.filter((group) => group.includes(q)).map((name) => ({ name })),
      );
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/groups\/[^/]+(\/members(\/[^/]+)?)?$/.test(url.pathname),
    (route) => {
      const request = route.request();
      const [, , , , group = "", , principal] = new URL(request.url()).pathname
        .split("/")
        .map(decodeURIComponent);
      const list = members.get(group);
      if (list === undefined) {
        return answer(route, { detail: `No group named '${group}'.`, type: "NotFoundError" }, 404);
      }
      if (principal === undefined) {
        if (request.method() === "DELETE") {
          groups.splice(groups.indexOf(group), 1);
          members.delete(group);
          return route.fulfill({ status: 204 });
        }
        return answer(
          route,
          [...list]
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((one) => ({ ...one, is_active: true, picture: null })),
        );
      }
      if (request.method() === "PUT") {
        list.push({ name: principal, type: "USER" });
      } else {
        list.splice(
          list.findIndex((one) => one.name === principal),
          1,
        );
      }
      return route.fulfill({ status: 204 });
    },
  );
  await page.route(
    (url) => url.pathname.startsWith("/api/v1/grants"),
    (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === "DELETE") {
        grants = grants.filter((one) => one.id !== url.pathname.split("/").at(-1));
        return route.fulfill({ status: 204 });
      }
      if (request.method() === "POST") {
        const body = request.postDataJSON() as Record<string, string | undefined>;
        const made = grant(
          `g-${String(grants.length + 4)}`,
          body.group === undefined ? { person: body.principal ?? "" } : { group: body.group },
          { role: body.role ?? null, action: body.action ?? null },
          {
            kind: body.kind ?? "INSTANCE",
            environment: body.environment ?? null,
            namespace: body.namespace ?? null,
          },
        );
        grants = [made, ...grants];
        return answer(route, made, 201);
      }
      const group = url.searchParams.get("group_name");
      const person = url.searchParams.get("principal_name");
      // The table page's Access tab asks for one table's grants: it has none here.
      if (url.searchParams.has("table")) return answer(route, []);
      return answer(
        route,
        grants.filter(
          (one) =>
            (group === null || one["group"] === group) &&
            (person === null || (one["principal"] as { name: string } | null)?.name === person),
        ),
      );
    },
  );
  await page.route("**/api/v1/principals/*/grants", (route) =>
    answer(route, [
      {
        id: "g-3",
        role: "ADMIN",
        action: null,
        kind: "INSTANCE",
        environment: null,
        namespace: null,
        table: null,
        granted_at: AT,
        note: null,
        group: null,
      },
      {
        id: "g-1",
        role: "ANALYST",
        action: null,
        kind: "ENVIRONMENT",
        environment: "dev",
        namespace: null,
        table: null,
        granted_at: AT,
        note: null,
        group: "analysts",
      },
    ]),
  );
  await page.route("**/api/v1/principals/admin/permissions*", (route) => {
    const environment = new URL(route.request().url()).searchParams.get("environment");
    const allowed =
      environment === null
        ? ["MANAGE_GROUP", "MANAGE_GRANT"]
        : ["READ_ENVIRONMENT", "SUBMIT_QUERY"];
    return answer(route, {
      principal: "admin",
      active: true,
      scope: {
        kind: environment === null ? "INSTANCE" : "ENVIRONMENT",
        environment,
        namespace: null,
        table: null,
      },
      permissions: [
        ...allowed.map((action) => ({ action, allowed: true, via: [], reason: null })),
        { action: "READ_QUERY_TEXT", allowed: false, via: [], reason: "No grant gives it." },
      ],
      note: null,
    });
  });
  await page.route("**/api/v1/principals/maria", (route) =>
    answer(route, {
      name: "maria",
      type: "USER",
      is_active: true,
      picture: null,
      held_admin: false,
    }),
  );
  await page.route("**/api/v1/principals/maria/passkeys", (route) => answer(route, []));
  await page.route("**/api/v1/principals/maria/sessions", (route) => answer(route, []));
}

/** The token the account page's Create a token answers with, shown once. */
export const NEW_TOKEN = "lak_e2e_shown_once_4f9c2a";

/** A secret's value a test types, which must reach the server's body and nowhere else. */
export const SECRET_VALUE = "s3cr3t-e2e-sentinel";

/** The personal tokens: the first page holds two, the second one. */
const PERSONAL = [
  { key_id: "k1", name: "laptop", owner: "maria" },
  { key_id: "k2", name: "nightly-export", owner: "ci-bot" },
  { key_id: "k3", name: "workstation", owner: "kim.schulz" },
];

/** The secrets' names, in each environment. */
const SECRET_NAMES = ["pypi-corp", "alerts-webhook", "warehouse", "prod-hook"];

/** The paths the tokens' and secrets' stubs answer. */
export const TOKENS_STUBBED = [
  "/api/v1/tokens",
  "/api/v1/task-tokens",
  "/api/v1/task-tokens/t1",
  "/api/v1/task-tokens/t2",
  ...PERSONAL.map((token) => `/api/v1/principals/${token.owner}/tokens/${token.key_id}`),
  "/api/v1/principals/admin/tokens",
  "/api/v1/principals/admin/tokens/k-own",
  "/api/v1/principals/admin/passkeys",
  "/api/v1/principals/admin/sessions",
  "/api/v1/principals/admin/tokens/k-new",
  ...["dev", "prod"].flatMap((environment) => [
    `/api/v1/environments/${environment}/secrets`,
    ...SECRET_NAMES.flatMap((name) => [
      `/api/v1/environments/${environment}/secrets/${name}`,
      `/api/v1/environments/${environment}/secrets/${name}/use`,
    ]),
  ]),
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/**
 * A personal token as the lists answer it.
 *
 * @param token - Its key id, name and owner.
 * @param token.key_id - Its key id.
 * @param token.name - Its name.
 * @param token.owner - Its owner.
 * @returns The token.
 */
function personal(token: { key_id: string; name: string; owner: string }): Record<string, unknown> {
  return {
    ...token,
    created_at: AT,
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
  };
}

/**
 * A secret as the secrets routes answer it.
 *
 * @param environment - Its environment.
 * @param name - Its name.
 * @param use - Its use and target.
 * @param use.use - The kind of use, or null.
 * @param use.target - The target's name, or null.
 * @returns The secret.
 */
function secret(
  environment: string,
  name: string,
  use: { use: string | null; target: string | null },
): Record<string, unknown> {
  return {
    id: `${environment}-${name}`,
    name,
    environment,
    created_by: "admin-id",
    created_at: AT,
    rotated_at: null,
    format_version: 2,
    key_id: "env:0123456789abcdef",
    use: use.use,
    use_target: use.target,
    use_target_url: use.target === null ? null : "https://pkgs.example.org/simple",
  };
}

/**
 * Answers the tokens' and the secrets' routes: everyone's personal tokens over two pages, two
 * task tokens (one of a job's run in `dev`, one minted by hand in `prod`), the administrator's
 * own tokens (Create a token answers {@link NEW_TOKEN}), and the secrets of `dev` and `prod`.
 * Revokes and writes change what the next read answers. Register it after `signInAsAdmin`.
 *
 * @param page - The page.
 */
export async function stubTokensSecrets(page: Page): Promise<void> {
  let tokens = PERSONAL.map(personal);
  let tasks: Record<string, unknown>[] = [
    {
      key_id: "t1",
      principal: "ci-bot",
      environment: "dev",
      asset_scope: ["gold.*"],
      issued_at: AT,
      expires_at: "2099-01-01T00:00:00Z",
      run_id: "8a1f4c2e-6b3d-4e5f-9a7b-0c1d2e3f4a5b",
      job: "daily_sales",
      asset: null,
    },
    {
      key_id: "t2",
      principal: "ci-bot",
      environment: "prod",
      asset_scope: ["silver.orders"],
      issued_at: AT,
      expires_at: "2099-01-01T00:00:00Z",
      run_id: null,
      job: null,
      asset: null,
    },
  ];
  const own = [personal({ key_id: "k-own", name: "laptop", owner: "admin" })];
  const secrets = new Map<string, Record<string, unknown>[]>([
    [
      "dev",
      [
        secret("dev", "pypi-corp", { use: "package_index", target: "corp" }),
        secret("dev", "alerts-webhook", { use: "environment", target: null }),
      ],
    ],
    ["prod", [secret("prod", "prod-hook", { use: null, target: null })]],
  ]);
  await page.route(
    (url) => url.pathname === "/api/v1/tokens",
    (route) => {
      const url = new URL(route.request().url());
      const owner = url.searchParams.get("owner");
      if (owner !== null && !tokens.some((token) => token["owner"] === owner)) {
        return answer(
          route,
          { detail: `No principal named '${owner}'.`, type: "NotFoundError" },
          404,
        );
      }
      const mine = tokens.filter((token) => owner === null || token["owner"] === owner);
      const second = url.searchParams.get("cursor") === "c2";
      const page = second ? mine.slice(2) : mine.slice(0, 2);
      return route.fulfill({
        json: page,
        headers: !second && mine.length > 2 ? { "X-Next-Cursor": "c2" } : {},
      });
    },
  );
  await page.route(
    (url) =>
      /^\/api\/v1\/principals\/[^/]+\/tokens\/[^/]+$/.test(url.pathname) &&
      !url.pathname.startsWith("/api/v1/principals/admin/"),
    (route) => {
      const key = new URL(route.request().url()).pathname.split("/").at(-1);
      tokens = tokens.filter((token) => token["key_id"] !== key);
      return route.fulfill({ status: 204 });
    },
  );
  await page.route(
    (url) => url.pathname.startsWith("/api/v1/task-tokens"),
    (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === "DELETE") {
        tasks = tasks.filter((task) => task["key_id"] !== url.pathname.split("/").at(-1));
        return route.fulfill({ status: 204 });
      }
      const environment = url.searchParams.get("environment");
      return answer(
        route,
        tasks.filter((task) => environment === null || task["environment"] === environment),
      );
    },
  );
  await page.route("**/api/v1/principals/admin/passkeys", (route) => answer(route, []));
  await page.route("**/api/v1/principals/admin/sessions", (route) => answer(route, []));
  await page.route(
    (url) => url.pathname.startsWith("/api/v1/principals/admin/tokens"),
    (route) => {
      const request = route.request();
      const key = new URL(request.url()).pathname.split("/")[6];
      if (request.method() === "POST") {
        const { name } = request.postDataJSON() as { name: string };
        own.unshift(personal({ key_id: "k-new", name, owner: "admin" }));
        return answer(route, { key_id: "k-new", name, token: NEW_TOKEN }, 201);
      }
      if (request.method() === "DELETE") {
        const found = own.find((token) => token["key_id"] === key);
        if (found !== undefined) found["revoked_at"] = "2026-11-18T09:00:00Z";
        return route.fulfill({ status: 204 });
      }
      return answer(route, own);
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+\/secrets(\/[^/]+(\/use)?)?$/.test(url.pathname),
    (route) => {
      const request = route.request();
      const [, , , , environment = "", , name, use] = new URL(request.url()).pathname
        .split("/")
        .map(decodeURIComponent);
      const list = secrets.get(environment) ?? [];
      if (name === undefined) {
        if (request.method() === "POST") {
          const body = request.postDataJSON() as {
            name: string;
            use?: { kind: string; target: string | null };
          };
          const made = secret(environment, body.name, {
            use: body.use?.kind ?? null,
            target: body.use?.target ?? null,
          });
          secrets.set(environment, [...list, made]);
          return answer(route, made, 201);
        }
        return answer(route, list);
      }
      const found = list.find((one) => one["name"] === name);
      if (found === undefined) {
        return answer(route, { detail: `No secret named '${name}'.`, type: "NotFoundError" }, 404);
      }
      if (request.method() === "DELETE") {
        secrets.set(
          environment,
          list.filter((one) => one !== found),
        );
        return route.fulfill({ status: 204 });
      }
      if (use !== undefined) {
        const body = request.postDataJSON() as { kind: string; target: string | null };
        found["use"] = body.kind;
        found["use_target"] = body.target;
      } else {
        found["rotated_at"] = "2026-11-18T09:00:00Z";
      }
      return answer(route, found);
    },
  );
}

/**
 * Answers whoami as the administrator's account without admin rights that manages the secrets
 * of `dev` only (and may read `prod`). Register it after `signInAsAdmin`.
 *
 * @param page - The page.
 */
export async function stubSecretsOfDevOnly(page: Page): Promise<void> {
  await page.route("**/api/v1/whoami", (route) =>
    answer(route, {
      name: "admin",
      type: "USER",
      task_token: null,
      ci_token: null,
      language: null,
      admin: false,
      has_access: true,
      may_deploy: false,
      secret_environments: ["dev"],
      manage_policy: false,
      picture: "e1",
    }),
  );
}

/** The text the stubbed export answers, with a sentinel that must stay nowhere on the page. */
export const EXPORTED = "version: 1\n# sentinel-e2e-7Qx\nmanages: [instance]\n";

/** An expression of 4,000 characters, the longest a statement may be by default. */
export const LONG_EXPRESSION = `region IN (${"'EU', ".repeat(570)}'US')`.slice(0, 4000);

/** The paths {@link stubPolicies} answers, beside those of `session.ts`. */
export const POLICIES_STUBBED = [
  "/api/v1/access/managed-scopes",
  "/api/v1/access/status",
  "/api/v1/access/export",
  "/api/v1/environments",
  "/api/v1/row-filters",
  "/api/v1/column-masks",
  ...["f-1", "f-2", "f-3", "f-4"].map((id) => `/api/v1/row-filters/${id}`),
  ...["m-1", "m-2", "m-3"].map((id) => `/api/v1/column-masks/${id}`),
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** Options of {@link stubPolicies}. */
export interface PoliciesStubOptions {
  /** The scopes an access file manages: `instance`, environments' names. */
  readonly managed?: readonly string[];
  /** Whether the managed scopes carry a change made by hand. */
  readonly overrides?: boolean;
  /** Whether a row filter's expression is 4,000 characters long. */
  readonly longExpression?: boolean;
  /** Whether the person is a policy manager who is no administrator. */
  readonly policyManager?: boolean;
}

/**
 * A row filter or column mask as the lists answer it.
 *
 * @param id - Its id.
 * @param who - The group, or the person or service account.
 * @param on - Its environment, namespace and table.
 * @param on.environment - The environment.
 * @param on.namespace - The dotted namespace.
 * @param on.table - The table.
 * @param expression - The expression.
 * @param column - A mask's column.
 * @returns The policy.
 */
function policy(
  id: string,
  who: { group: string } | { person: string; service?: boolean },
  on: { environment: string; namespace: string; table: string },
  expression: string,
  column?: string,
): Record<string, unknown> {
  const person = "person" in who ? who : null;
  return {
    id,
    principal_id: person === null ? null : `${person.person}-id`,
    group_id: person === null ? "analysts-id" : null,
    principal:
      person === null
        ? null
        : {
            id: `${person.person}-id`,
            name: person.person,
            principal_type: person.service === true ? "service" : "user",
          },
    group: "group" in who ? who.group : null,
    ...on,
    expression,
    created_by: "admin-id",
    created_at: AT,
    bound: true,
    ...(column === undefined ? {} : { column }),
  };
}

/**
 * Answers Row filters & masks and Access file: three row filters (the list in two pages) and two
 * column masks on dev and prod, the managed scopes, the access files' status and the export;
 * writes change what the next read answers. Register it after `signInAsAdmin`.
 *
 * @param page - The page.
 * @param options - The managed scopes, the hand changes, the long expression and the person.
 */
export async function stubPolicies(page: Page, options: PoliciesStubOptions = {}): Promise<void> {
  const managed = options.managed ?? [];
  const station = { environment: "dev", namespace: "gold", table: "station_ranking" };
  const sales = { environment: "prod", namespace: "gold.sales", table: "fact_sales" };
  let filters = [
    policy("f-1", { person: "maria" }, station, "region = 'EU'"),
    policy("f-2", { group: "analysts" }, sales, "store_id IN (1, 2, 3)"),
    policy(
      "f-3",
      { person: "ci-bot", service: true },
      station,
      options.longExpression === true ? LONG_EXPRESSION : "has_group('ops')",
    ),
  ];
  let masks = [
    policy("m-1", { group: "analysts" }, station, "'***' || substr(email, -4)", "email"),
    policy("m-2", { person: "maria" }, sales, "md5(customer_email)", "customer_email"),
  ];
  if (options.policyManager === true) {
    await page.route("**/api/v1/whoami", (route) =>
      answer(route, {
        name: "admin",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: false,
        has_access: true,
        may_deploy: false,
        secret_environments: [],
        manage_policy: true,
        picture: "e1",
      }),
    );
  }
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    (route) =>
      answer(
        route,
        ["dev", "prod"].map((name) => ({
          name,
          storage_root: `file:///srv/lakana/${name}`,
          storage_secret_ref: null,
          is_protected: name === "prod",
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      ),
  );
  await page.route("**/api/v1/access/managed-scopes", (route) =>
    answer(
      route,
      managed.map((scope) => ({ scope, file_name: "access.yaml", applied_at: AT })),
    ),
  );
  await page.route("**/api/v1/access/status", (route) =>
    answer(route, {
      scopes: managed.map((scope) => ({
        scope,
        file_name: "access.yaml",
        file_sha256: "3f2a9b8c7d6e5f4a3b2c1d0e",
        source_commit: "0123456789abcdef0123",
        applied_by: "admin",
        applied_at: AT,
      })),
      overrides:
        options.overrides === true
          ? [
              {
                scope: managed[0] ?? "instance",
                change: "grant VIEWER on prod to user:bob",
                reason: "incident 42",
                by: "admin",
                at: AT,
              },
            ]
          : [],
    }),
  );
  await page.route("**/api/v1/access/export*", (route) => answer(route, { text: EXPORTED }));
  /**
   * Serves one kind of policy: the list (the row filters in two pages), a new one and a delete.
   *
   * @param path - The list's path.
   * @param read - The policies now.
   * @param write - Replaces them.
   * @param paged - Whether the list comes in two pages.
   */
  const serve = async (
    path: string,
    read: () => Record<string, unknown>[],
    write: (next: Record<string, unknown>[]) => void,
    paged: boolean,
  ): Promise<void> => {
    await page.route(
      (url) => url.pathname === path || url.pathname.startsWith(`${path}/`),
      (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.method() === "DELETE") {
          write(read().filter((one) => one["id"] !== url.pathname.split("/").at(-1)));
          return route.fulfill({ status: 204 });
        }
        if (request.method() === "POST") {
          const body = request.postDataJSON() as Record<string, string | null>;
          const made = policy(
            path.endsWith("masks") ? "m-3" : "f-4",
            body["group"] === null
              ? { person: body["principal"] ?? "" }
              : { group: body["group"] ?? "" },
            {
              environment: body["environment"] ?? "",
              namespace: body["namespace"] ?? "",
              table: body["table"] ?? "",
            },
            body["expression"] ?? "",
            body["column"] ?? undefined,
          );
          write([made, ...read()]);
          return answer(route, made, 201);
        }
        const environment = url.searchParams.get("environment");
        const found = read().filter(
          (one) => environment === null || one["environment"] === environment,
        );
        if (!paged || environment !== null) return answer(route, found);
        if (url.searchParams.get("cursor") === "c2") return answer(route, found.slice(2));
        return route.fulfill({
          status: 200,
          json: found.slice(0, 2),
          ...(found.length > 2 ? { headers: { "X-Next-Cursor": "c2" } } : {}),
        });
      },
    );
  };
  await serve(
    "/api/v1/row-filters",
    () => filters,
    (next) => {
      filters = next;
    },
    true,
  );
  await serve(
    "/api/v1/column-masks",
    () => masks,
    (next) => {
      masks = next;
    },
    false,
  );
}

/** A CA bundle path of over 300 characters: its value must wrap inside its cell. */
export const CA_BUNDLE = `/etc/ssl/${"certs/".repeat(48)}ca.pem`;

/** The link of a new invite, shown once. */
export const INVITE_URL = "http://localhost:4173/sign-in#l=pub-e2e&s=sec-e2e-shown-once";

/**
 * The paths the Sign-in and Configuration stubs answer, beside those of `session.ts`, and the
 * step-up pair that `stubStepUpCeremony` (`manage.ts`) answers.
 */
export const SIGN_IN_STUBBED = [
  "/api/v1/settings/configuration",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
  "/api/v1/principals/kim.schulz",
  "/api/v1/principals/kim.schulz/recovery-link",
];

/**
 * The server's settings as `GET /api/v1/settings/configuration` answers them: a dozen across
 * three groups and General, with the secrets as the server sends them (no value), a list, an
 * object, a setting without a value and a long path.
 */
export const CONFIGURATION = [
  { name: "LAKANA_DATABASE__URL", value: null, secret: true, has_value: true, source: "set" },
  {
    name: "LAKANA_DATABASE__ECHO",
    value: false,
    secret: false,
    has_value: true,
    source: "default",
  },
  {
    name: "LAKANA_BOOTSTRAP_ADMIN",
    value: "ops-admin",
    secret: false,
    has_value: true,
    source: "set",
  },
  { name: "LAKANA_SECRETS__MASTER_KEY", value: null, secret: true, has_value: true, source: "set" },
  {
    name: "LAKANA_RUN__LEASE_SECONDS",
    value: 60,
    secret: false,
    has_value: true,
    source: "default",
  },
  {
    name: "LAKANA_RUN__HTTPS_PROXY",
    value: null,
    secret: true,
    has_value: false,
    source: "default",
  },
  { name: "LAKANA_RUN__NO_PROXY", value: null, secret: false, has_value: false, source: "default" },
  {
    name: "LAKANA_RUN__CA_BUNDLE",
    value: CA_BUNDLE,
    secret: false,
    has_value: true,
    source: "set",
  },
  {
    name: "LAKANA_RUN__INGESTION_ROOTS",
    value: { raw: "/srv/lakana/raw", landing: "/srv/lakana/landing" },
    secret: false,
    has_value: true,
    source: "set",
  },
  { name: "LAKANA_CI_OIDC__PROXY", value: null, secret: true, has_value: false, source: "default" },
  {
    name: "LAKANA_CI_OIDC__ALGORITHMS",
    value: ["RS256"],
    secret: false,
    has_value: true,
    source: "default",
  },
  { name: "LAKANA_PORT", value: 8000, secret: false, has_value: true, source: "set" },
];

/** One open invite and one open recovery link, each with a long name (a 300-character name). */
const OPEN_INVITE = {
  id: "i-1",
  name: LONG_NAME,
  display_name: "Maria Lopez",
  created_at: "2026-11-18T09:00:00Z",
  expires_at: "2026-11-25T09:00:00Z",
};
const OPEN_LINK = {
  id: "r-1",
  name: LONG_NAME,
  created_at: "2026-11-18T09:00:00Z",
  expires_at: "2026-11-18T10:00:00Z",
};

/**
 * Stubs Settings › Sign-in's writes and lists and Configuration's read, over `signInAsAdmin`'s:
 * the session lengths saved as sent, an invite and a recovery link for kim.schulz issued and
 * then listed, and the configuration.
 *
 * @param page - The page.
 */
export async function stubSignInAndConfiguration(page: Page): Promise<void> {
  let lengths = {
    idle_timeout_minutes: 720,
    absolute_lifetime_minutes: 10080,
    bounds: {
      idle_timeout_minutes_min: 15,
      idle_timeout_minutes_max: 1440,
      absolute_lifetime_minutes_min: 60,
      absolute_lifetime_minutes_max: 43200,
    },
  };
  const invites: unknown[] = [OPEN_INVITE];
  const links: unknown[] = [OPEN_LINK];
  await page.route("**/api/v1/settings/sign-in", async (route) => {
    if (route.request().method() === "PATCH") {
      lengths = { ...lengths, ...(route.request().postDataJSON() as object) };
    }
    await route.fulfill({ json: lengths });
  });
  await page.route("**/api/v1/invites", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fulfill({ json: invites });
      return;
    }
    const body = route.request().postDataJSON() as { name: string; display_name: string | null };
    const invite = {
      id: "i-2",
      name: body.name,
      display_name: body.display_name,
      created_at: "2026-11-19T09:00:00Z",
      expires_at: "2026-11-26T09:00:00Z",
    };
    invites.push(invite);
    await route.fulfill({
      status: 201,
      json: {
        id: invite.id,
        name: invite.name,
        display_name: invite.display_name,
        url: INVITE_URL,
        expires_at: invite.expires_at,
      },
    });
  });
  await page.route("**/api/v1/recovery-links", (route) => route.fulfill({ json: links }));
  await page.route("**/api/v1/principals/kim.schulz", (route) =>
    route.fulfill({
      json: { name: "kim.schulz", type: "USER", is_active: true, picture: null, held_admin: false },
    }),
  );
  await page.route("**/api/v1/principals/kim.schulz/recovery-link", async (route) => {
    const link = {
      id: "r-2",
      name: "kim.schulz",
      created_at: "2026-11-19T09:00:00Z",
      expires_at: "2026-11-19T10:00:00Z",
    };
    links.push(link);
    await route.fulfill({
      status: 201,
      json: { id: link.id, name: link.name, url: INVITE_URL, expires_at: link.expires_at },
    });
  });
  await stubConfiguration(page);
}

/**
 * Stubs Configuration's read.
 *
 * @param page - The page.
 * @param settings - The settings it answers; {@link CONFIGURATION} by default.
 */
export async function stubConfiguration(
  page: Page,
  settings: readonly unknown[] = CONFIGURATION,
): Promise<void> {
  await page.route("**/api/v1/settings/configuration", (route) =>
    route.fulfill({ json: settings }),
  );
}
