/**
 * The hidden-character check (#669): every stubbed route answers with a right-to-left override
 * (U+202E) at the end of each text field, and the page must show it only as its mark `U+202E`,
 * never as the character itself, in its text, its accessible names and its titles. A screen that
 * renders a server value without `VisibleText`, `markedText`, `t()` or `problemText` fails it.
 *
 * @packageDocumentation
 */
import { expect, type Locator, type Page, type Request, type Route } from "@playwright/test";

import { apiTypes, typeOf } from "./api-types";

/** The right-to-left override, the character a spoofed name hides behind. */
export const RLO = "‮";

/** The override as it appears in a URL. */
const RLO_ENCODED = /%E2%80%AE/gi;

/** The answer types of `src/api`, read from the source ({@link apiTypes}). */
const API_TYPES = apiTypes();

/**
 * Fields `src/api` types as `string` but documents as a code the interface translates (`item`,
 * `causes`, `via`, a check's `outcome`), by type. An override in one sends the screen down a
 * branch the server never takes, which the named values of a step then miss. A code the interface
 * compares is a union of string literals in `src/api`, which the check finds by type. Every other
 * `string` field carries an override, even where the same name is a code in another type.
 */
export const STRING_CODES: Readonly<Record<string, readonly string[]>> = {
  BrowserSessionOut: ["via"],
  CheckHealthOut: ["outcome"],
  HealthSummaryOut: ["item"],
  InstanceStatusOut: ["causes"],
  SessionOut: ["via"],
};

/**
 * The type each object of the stubbed answers is read as, by its keys (sorted, joined by a
 * space): the pick of {@link typeOf}, `-` for none. `typeOf` falls back to a partial fit and
 * breaks ties by declaration order, so a new type in `src/api` or a changed stub can move an
 * object to a type whose codes include one of its free-text fields, which would then carry no
 * override. Each object `poison` reads must be here ({@link misreadObjects}), and each entry must
 * still be what `typeOf` picks (`hidden-characters.spec.ts`).
 */
export const READ_AS: Readonly<Record<string, string>> = {
  "": "-",
  "absolute_expires_at browser created_at current id last_seen_at peer user_agent via":
    "BrowserSessionOut",
  "absolute_expires_at display_name name needs_passkey offer_passkey step_up_until type via":
    "SessionOut",
  "action allowed reason via": "PermissionOut",
  "action environment granted_at granted_by group group_id id kind namespace note principal principal_id role table":
    "GrantOut",
  "actions commit_sha created_at created_by deleted_at deleted_by deleted_versions file_count files is_directory manifest_sha256 project_version_id run_id size_bytes task_run_id version":
    "ArtifactVersionOut",
  "actions count name namespaces": "TableTreeOut.environments",
  "actions count path": "TableTreeOut.environments.namespaces",
  "actions created_at current_version defined_by keep_versions kind name namespace size_bytes uuid versions":
    "ArtifactOut",
  "actions created_at defined_by description description_language description_source expires_at expires_soon file_count health health_missing health_never_judged health_summary hidden_references kind label label_language label_source language last_read_at last_write_at main_language metadata_location model name namespace owner readers_7d references row_count schema size_bytes snapshot_id sql state state_message terms translations uuid":
    "TableOut",
  "actions dropped_at kind name namespace purge_after restorable uuid": "DroppedTableOut",
  "actions exists kind repository table": "OrphanOut",
  "active note permissions principal scope": "PermissionsOut",
  "admin ci_token has_access language may_deploy name picture task_token type": "WhoamiOut",
  "alert band column effect id last_fired_at observed observed_at signal snapshot_id state values_hidden":
    "HealthSignalOut",
  "alert band column effect id observed observed_at signal snapshot_id state values_hidden":
    "HealthSignalOut",
  "artifact created_at expires_at upload_id": "ArtifactUploadOut",
  "at column id table target target_column target_exists": "ModelReferenceOut",
  "at environment kind ref title": "AttentionItemOut",
  "audience complete hidden local steps": "GetStartedOut",
  "backup_eligible created_at disabled_reason id last_used_at name usable_here": "PasskeyOut",
  binary: "-",
  "bound column created_at created_by environment expression group group_id id namespace principal principal_id table":
    "ColumnMaskOut",
  "bound created_at created_by environment expression group group_id id namespace principal principal_id table":
    "PolicyOut",
  "browse_url bytes commit content defined deployed_at line message path project_version_id reason repository":
    "TableDefinitionFileOut",
  "browse_url commit defined deployed_at line path project_version_id repository":
    "TableDefinitionOut",
  "capped items next_cursor": "TableListPageOut",
  "causes detail state workers_alive workers_total": "InstanceStatusOut",
  "check error failing_rows finished_at kept name not_published on_failure outcome rollback_outcome run_id":
    "CheckHealthOut",
  "checks days hidden_checks last_maintenance readers signals": "TableHealthOut",
  "checks grain hidden_references key key_none kind measures references": "TableModelOut",
  "column column_type kind language matched snippet translated": "TableMatchOut",
  "column table": "MaskedColumnOut",
  "columns columns_cut duration_ms masked_columns masked_columns_cut next_offset preview row_count row_cut rows":
    "ResultPageOut",
  "connection_kind distance id kind name": "GraphNodeOut",
  "count environments": "TableTreeOut",
  "created_at display_name expires_at id name": "OpenInviteOut",
  "created_at environment health_summary kind last_read_at last_write_at match model_kind name namespace owner readers_7d row_count size_bytes state uuid":
    "TableListItemOut",
  "created_at expires_at id name": "OpenRecoveryLinkOut",
  "created_at finished_at host_keys id kind message outcome state": "RepositoryProbeOut",
  "created_at id kind subject": "NoticeOut",
  "days expires_at kept last_activity_at name namespace never on set_at set_by source uuid":
    "TableRetentionOut",
  "description description_language description_source label label_language label_source name nullable terms type":
    "ColumnOut",
  detail: "ErrorBody",
  "detail type": "ErrorBody",
  "display_name kind name state": "LinkDescriptionOut",
  "distance id kind name": "GraphNodeOut",
  "distance id kind name repository": "GraphNodeOut",
  "done id": "GetStartedStepOut",
  "edges error nodes permissions": "GraphOut",
  "edges next_cursor nodes": "GraphOut",
  "environment fingerprint made public_key": "DeployKeyMadeOut",
  "environment health kind last_write_at name namespace owner state uuid": "TableListItemOut",
  "environment job last_run_at last_run_status repository": "WorkJobOut",
  "environment kind namespace table": "ScopeOut",
  "environment kind opened_at ref": "RecentOut",
  "environment last_deploy_at last_deploy_status": "WorkEnvironmentOut",
  "environments name": "WorkRepositoryOut",
  "fingerprint line type": "HostKeyOut",
  "get_started_hidden tour_seen update_note_seen": "InterfaceStateOut",
  "grant_id group kind role scope": "ViaOut",
  "hidden next_cursor orphans": "-",
  "hidden_incoming hidden_outgoing incoming outgoing table": "TableReferencesOut",
  high: "-",
  "high low max min": "-",
  "high low median": "-",
  "id name principal_type": "SubjectPrincipalOut",
  "is_active name picture type": "PrincipalOut",
  "is_protected name": "OwnerOut",
  "item others state text": "HealthSummaryOut",
  "items next_cursor": "TableListPageOut",
  "items truncated": "AttentionOut",
  "job last_commit_at seconds source": "-",
  "jobs repositories": "WorkOut",
  "keys notes replaced secret skipped": "DeployKeyOut",
  "kind lower upper": "-",
  "kind name": "OwnerOut",
  "kind source target": "GraphEdgeOut",
  "last_read_at principal read_count": "ReaderOut",
  "memory_limit_bytes memory_used_bytes slots_busy slots_total workers": "StatusDetailOut",
  name: "OwnerOut",
  "name type": "ResultColumnOut",
  "nulls rate values": "-",
  "path properties": "NamespaceOut",
  "path repository": "DefinedByOut",
  "path sha256 size": "ArtifactFileOut",
  query_id: "QueryStatusOut",
  "query_id status": "QueryStatusOut",
  table: "MaskedColumnOut",
  version: "InfoOut",
};

/** The objects `poison` read since {@link misreadObjects} last ran: their keys, and the type. */
const reads = new Map<string, string>();

/**
 * The objects `poison` read as another type than {@link READ_AS} pins, or that it does not
 * list, since the last call; then forgets them.
 *
 * @returns One line for each: its keys, the type it was read as and the one pinned.
 */
export function misreadObjects(): string[] {
  const found = [...reads]
    .filter(([keys, name]) => READ_AS[keys] !== name)
    .map(([keys, name]) => `"${keys}": "${name}" (pinned: ${READ_AS[keys] ?? "none"})`)
    .sort();
  reads.clear();
  return found;
}

/**
 * The fields of an answer's object that hold codes: those of its type ({@link typeOf}) whose
 * type is a union of string literals, and its {@link STRING_CODES}. Records the pick for
 * {@link misreadObjects}.
 *
 * @param keys - The object's keys.
 * @returns The fields.
 */
function codeFields(keys: readonly string[]): ReadonlySet<string> {
  const type = typeOf(API_TYPES, keys);
  reads.set([...keys].sort().join(" "), type?.name ?? "-");
  return type === undefined
    ? new Set()
    : new Set([...type.literal, ...(STRING_CODES[type.name] ?? [])]);
}

/**
 * A code by its shape: an upper-case constant (`USER`, `READ_TABLE`, `SUBMIT_QUERY`), or a time
 * as the server writes it, which the interface parses and formats.
 */
const CODE_VALUE = /^([A-Z][A-Z0-9_]*|\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:\d{2})?)?)$/;

/** An error's type, such as `StepUpRequired`, which the interface compares. */
const ERROR_TYPE = /^[A-Z][A-Za-z]+$/;

/** A field that holds an id the server made: `id`, `grant_id`, `query_id`. */
const ID_FIELD = /^(.+_)?id$/;

/**
 * Whether a string is a code the interface compares, looks up or parses, never shows: an id, or
 * a reference to one elsewhere in the answer (a lineage edge's ends), included. An override in
 * one would only send the screen down a branch the server never takes, so it carries none; every
 * other string does.
 *
 * @param value - The string.
 * @param field - The field that holds it.
 * @param codes - The code fields of the object that holds it ({@link codeFields}).
 * @param ids - The ids of the answer.
 * @returns Whether it is a code.
 */
function isCode(
  value: string,
  field: string,
  codes: ReadonlySet<string>,
  ids: ReadonlySet<string>,
): boolean {
  return (
    value === "" ||
    ids.has(value) ||
    codes.has(field) ||
    ID_FIELD.test(field) ||
    CODE_VALUE.test(value) ||
    (field === "type" && ERROR_TYPE.test(value))
  );
}

/**
 * The ids in an answer: every string of a field {@link ID_FIELD} matches.
 *
 * @param value - The answer.
 * @param field - The field that holds it.
 * @param ids - The ids found so far; filled.
 * @returns The ids.
 */
function idsOf(value: unknown, field = "", ids = new Set<string>()): Set<string> {
  if (typeof value === "string" && ID_FIELD.test(field)) {
    ids.add(value);
  } else if (Array.isArray(value)) {
    value.forEach((item: unknown) => idsOf(item, field, ids));
  } else if (value !== null && typeof value === "object") {
    Object.entries(value).forEach(([key, item]) => idsOf(item, key, ids));
  }
  return ids;
}

/**
 * The answer with an override at the end of every text but the codes ({@link isCode}), each
 * object read as the `src/api` type it fits ({@link typeOf}).
 *
 * @param answer - The answer as a stub wrote it.
 * @returns The poisoned answer.
 */
export function poison(answer: unknown): unknown {
  const ids = idsOf(answer);
  const walk = (value: unknown, field: string, codes: ReadonlySet<string>): unknown => {
    if (typeof value === "string") {
      return isCode(value, field, codes, ids) ? value : value + RLO;
    }
    if (Array.isArray(value)) {
      return value.map((item: unknown) => walk(item, field, codes));
    }
    if (value !== null && typeof value === "object") {
      const own = codeFields(Object.keys(value));
      return Object.fromEntries(
        Object.entries(value).map(([key, item]) => [key, walk(item, key, own)]),
      );
    }
    return value;
  };
  return walk(answer, "", new Set());
}

/**
 * The value with every override taken out: what the stub expects in an address or a body the
 * page sent back with a poisoned name in it.
 *
 * @param value - A string, or a parsed body.
 * @returns The value as the stub wrote it.
 */
function clean<T>(value: T): T {
  if (typeof value === "string") {
    return value.replace(RLO_ENCODED, "").replaceAll(RLO, "") as T;
  }
  if (Array.isArray(value)) {
    return value.map((item: unknown) => clean(item)) as T;
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clean(item)])) as T;
  }
  return value;
}

/**
 * A glob of `page.route` as a regular expression: `**` any text, `*` any text but `/`.
 *
 * @param glob - The glob.
 * @returns The expression, over the whole URL.
 */
function globPattern(glob: string): RegExp {
  const source = glob
    .split("**")
    .map((part) =>
      part
        .split("*")
        .map((piece) => piece.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
        .join("[^/]*"),
    )
    .join(".*");
  return new RegExp(`^${source}$`);
}

/** What `page.route` takes to match a request. */
type Matcher = Parameters<Page["route"]>[0];

/** What `page.route` takes to answer a request. */
type Handler = (route: Route, request: Request) => unknown;

/**
 * The request as the stub expects it: its URL and body without the overrides.
 *
 * @param request - The request the page sent.
 * @returns The request, cleaned where a stub reads it.
 */
function cleanRequest(request: Request): Request {
  return new Proxy(request, {
    get(target, property) {
      if (property === "url") {
        return () => clean(target.url());
      }
      if (property === "postData") {
        return () => clean(target.postData());
      }
      if (property === "postDataJSON") {
        return () => clean(target.postDataJSON() as unknown);
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * The route with its answer poisoned and its request cleaned.
 *
 * @param route - The route the stub answers.
 * @returns The route the stub sees.
 */
function poisonRoute(route: Route): Route {
  return new Proxy(route, {
    get(target, property) {
      if (property === "fulfill") {
        return (options: Parameters<Route["fulfill"]>[0] = {}) =>
          target.fulfill(
            options.json === undefined ? options : { ...options, json: poison(options.json) },
          );
      }
      if (property === "request") {
        return () => cleanRequest(target.request());
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * The page as a stub sees it: each `page.route` it adds matches the address without the
 * overrides and answers with every text field poisoned ({@link poison}). Hand it to the stubs
 * of `session.ts`, `manage.ts`, `places.ts` and `artifacts.ts` unchanged.
 *
 * @param page - The page under test.
 * @returns The page to stub.
 */
export function poisoned(page: Page): Page {
  const route = (matcher: Matcher, handler: Handler): ReturnType<Page["route"]> => {
    const matches = (url: URL): boolean => {
      const plain = new URL(clean(url.href));
      if (typeof matcher === "function") {
        return matcher(plain);
      }
      // A RegExp and a URLPattern both test a URL's text.
      return (typeof matcher === "string" ? globPattern(matcher) : matcher).test(plain.href);
    };
    return page.route(matches, (target, request) =>
      handler(poisonRoute(target), cleanRequest(request)),
    );
  };
  return new Proxy(page, {
    get(target, property) {
      if (property === "route") {
        return route;
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * Every place on the page that shows the override itself: a text node (an `<svg>`'s `<title>`
 * included), an attribute that names or describes an element, the text of an
 * `<input type="button">`, `submit` or `reset`, or the document's title. A form field's own value
 * is left out, as a person edits the text as it is. The walk does not enter shadow roots: the
 * interface attaches none (no `attachShadow` in `src/`).
 *
 * @param page - The page.
 * @returns Where each one is, with the override written `⟨RLO⟩`.
 */
export function rawOverrides(page: Page): Promise<string[]> {
  return page.evaluate((rlo) => {
    const ATTRIBUTES = [
      "aria-label",
      "aria-description",
      "aria-roledescription",
      "aria-valuetext",
      "aria-placeholder",
      "title",
      "alt",
      "placeholder",
      "label",
      "aria-braillelabel",
      "aria-brailleroledescription",
    ];
    /**
     * A short path to an element, for the report.
     *
     * @param element - The element.
     * @returns Its tag, id or classes, and those of two ancestors.
     */
    function where(element: Element | null): string {
      const parts: string[] = [];
      for (let node = element; node !== null && parts.length < 3; node = node.parentElement) {
        const id = node.id === "" ? "" : `#${node.id}`;
        const classes = [...node.classList]
          .slice(0, 2)
          .map((name) => `.${name}`)
          .join("");
        parts.unshift(`${node.tagName.toLowerCase()}${id}${classes}`);
      }
      return parts.join(" > ");
    }
    const shown = (text: string): string => text.replaceAll(rlo, "⟨RLO⟩").trim().slice(0, 120);
    const found: string[] = [];
    if (document.title.includes(rlo)) {
      found.push(`document title: ${shown(document.title)}`);
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (node.nodeValue?.includes(rlo) === true && parent?.closest("textarea") == null) {
        found.push(`text in ${where(parent)}: ${shown(node.nodeValue)}`);
      }
    }
    for (const element of document.body.querySelectorAll("*")) {
      for (const name of ATTRIBUTES) {
        const value = element.getAttribute(name);
        if (value?.includes(rlo) === true) {
          found.push(`${name} of ${where(element)}: ${shown(value)}`);
        }
      }
    }
    for (const input of document.body.querySelectorAll<HTMLInputElement>(
      "input[type=button], input[type=submit], input[type=reset]",
    )) {
      if (input.value.includes(rlo)) {
        found.push(`value of ${where(input)}: ${shown(input.value)}`);
      }
    }
    return found;
  }, RLO);
}

/**
 * Asserts that the screen shows each of the stubbed values it names with its mark, inside the
 * open dialog or else `<main>` (the shell's own marks, such as the user menu's name, do not
 * count), so a step whose screen never got its poisoned answer fails; and that the page shows no
 * override itself, softly, so one run names every screen that does.
 *
 * @param page - The page.
 * @param label - What the page shows, for the message.
 * @param values - Stubbed values the screen shows, as the stubs write them.
 */
export async function expectMarked(
  page: Page,
  label: string,
  values: readonly [string, ...string[]],
): Promise<void> {
  await page.waitForLoadState("networkidle");
  const dialog = page.getByRole("dialog");
  const scope: Locator = (await dialog.count()) > 0 ? dialog.last() : page.locator("main");
  for (const value of values) {
    await expect(scope, `${label}: ${value} shown with its mark`).toContainText(`${value}U+202E`);
  }
  await expectNoRaw(page, label);
}

/**
 * Asserts, softly, that the page shows no override itself: for a step whose own part (an open
 * menu of the interface's words) shows no stubbed value.
 *
 * @param page - The page.
 * @param label - What the page shows, for the message.
 */
export async function expectNoRaw(page: Page, label: string): Promise<void> {
  expect.soft(await rawOverrides(page), `${label}: a hidden character shown as itself`).toEqual([]);
}

/**
 * Records the paths of the API requests the page sends, the overrides taken out, so a test can
 * require that each one was stubbed: an unstubbed request would reach `vite preview` and answer
 * with no poisoned text.
 *
 * @param page - The page.
 * @returns The paths, filled as requests go out.
 */
export function apiPaths(page: Page): string[] {
  const paths: string[] = [];
  page.on("request", (request) => {
    const path = clean(new URL(request.url()).pathname);
    if (path.startsWith("/api/")) {
      paths.push(path);
    }
  });
  return paths;
}
