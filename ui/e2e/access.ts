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
