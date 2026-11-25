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
 * `causes`, `via`, a check's `outcome`, a failure's `code`, a job's notification `events`, an
 * attempt's `outcome`), by type. An override in one sends the screen down a branch the server
 * never takes, which the named values of a step then miss. A code the interface compares is a
 * union of string literals in `src/api`, which the check finds by type. Every other `string`
 * field carries an override, even where the same name is a code in another type.
 */
export const STRING_CODES: Readonly<Record<string, readonly string[]>> = {
  BrowserSessionOut: ["via"],
  CheckHealthOut: ["outcome"],
  FailureOut: ["code"],
  HealthSummaryOut: ["item"],
  InstanceStatusOut: ["causes"],
  NotifyOut: ["events"],
  SessionOut: ["via"],
  TaskAttemptOut: ["outcome"],
};

/**
 * The type each object of the stubbed answers is read as, by its keys ({@link readKey}): the pick
 * of {@link typeOf}, `-` for none. `typeOf` falls back to a partial fit and breaks ties by
 * declaration order, so a new type in `src/api` or a changed stub can move an object to a type
 * whose codes include one of its free-text fields, which would then carry no override.
 * `hidden-characters.spec.ts` fails on an object a stub serves that is read as another type or
 * not listed ({@link takeReads}), on an entry `typeOf` no longer picks, and on an entry no stub
 * serves.
 */
export const READ_AS: Readonly<Record<string, string>> = {
  '["display_name","enabled"]': "OidcInfoOut",
  '["absolute_lifetime_minutes","bounds","idle_timeout_minutes","provider","token_provider_window_days"]':
    "SignInSettingsOut",
  '["absolute_lifetime_minutes_max","absolute_lifetime_minutes_min","idle_timeout_minutes_max","idle_timeout_minutes_min","token_provider_window_days_max","token_provider_window_days_min"]':
    "SignInBoundsOut",
  '["display_name","issuer","linked_people","unlinked_admins"]': "SignInProviderOut",
  '["held_admin","is_active","name","picture","provider_link","type"]': "PrincipalOut",
  '["email","issuer","last_sign_in_at","name","token_window_ends_at"]': "ProviderLinkOut",
  '["age_seconds","host","requested_at","requested_from"]': "CliDeviceHostOut",
  '["active_runs","heartbeat_at","holder","pending","ready","running"]': "SchedulerOut",
  '["created_at","created_by","credential_secret","credential_username","default_ref","deploy_keys","host_keys","id","kind","name","ssh_known_hosts","updated_at","url"]':
    "RepositoryOut",
  '["deploy_run_id","heartbeat_at","host","id","memory","query_run_id","replaced","started_at","status","takes","task_run_id","version"]':
    "StatusWorkerOut",
  '["environment","fingerprint","public_key"]': "RepositoryDeployKeyOut",
  '["fingerprint","marker","type"]': "TrustedHostKeyOut",
  '["limit_bytes","measured_at","used_bytes"]': "WorkerMemoryOut",
  '["absolute_expires_at","browser","created_at","current","id","last_seen_at","peer","user_agent","via"]':
    "BrowserSessionOut",
  '["absolute_expires_at","display_name","name","needs_passkey","offer_passkey","step_up_until","type","via"]':
    "SessionOut",
  '["access_key_id","prefix"]': "-",
  '["action","allowed","reason","via"]': "PermissionOut",
  '["action","environment","granted_at","granted_by","group","group_id","id","kind","namespace","note","principal","principal_id","role","table"]':
    "GrantOut",
  '["actions","cancel_requested","commit_sha","created_at","ended_tasks","environment","error_message","failed_tasks","finished_at","id","job","kind","repository","resumed_from_run_id","selection","started_at","status","tasks","total_tasks","trigger","triggered_by"]':
    "RunOut",
  '["actions","commit_sha","created_at","created_by","deleted_at","deleted_by","deleted_versions","file_count","files","is_directory","manifest_sha256","project_version_id","run_id","size_bytes","task_run_id","version"]':
    "ArtifactVersionOut",
  '["actions","address","created_at","created_by","declared_by","environment","kind","last_test","name","secret","settings","updated_at"]':
    "ConnectionOut",
  '["actions","name"]': "ConnectionEnvironmentOut",
  '["actions","commit_sha","cron","downstream","environment","last_run_at","last_run_id","last_run_status","name","next_run_at","notify","paused","repository","select","source_url","state","timezone","triggers","upstream","waiting_for","waits_for_run_id"]':
    "JobOut",
  '["actions","count","name","namespaces"]': "TableTreeOut.environments",
  '["actions","count","path"]': "TableTreeOut.environments.namespaces",
  '["actions","created_at","current_version","defined_by","keep_versions","kind","name","namespace","size_bytes","uuid","versions"]':
    "ArtifactOut",
  '["actions","created_at","defined_by","description","description_language","description_source","expires_at","expires_soon","file_count","health","health_missing","health_never_judged","health_summary","hidden_references","kind","label","label_language","label_source","language","last_read_at","last_write_at","main_language","metadata_location","model","name","namespace","owner","readers_7d","references","row_count","schema","size_bytes","snapshot_id","sql","state","state_message","terms","translations","uuid"]':
    "TableOut",
  '["actions","dropped_at","kind","name","namespace","purge_after","restorable","uuid"]':
    "DroppedTableOut",
  '["actions","exists","kind","repository","table"]': "OrphanOut",
  '["active","note","permissions","principal","scope"]': "PermissionsOut",
  '["admin","ci_token","has_access","instance_actions","language","manage_policy","managed_environments","may_deploy","name","picture","reads_environments","secret_environments","task_token","type"]':
    "WhoamiOut",
  '["artifacts","code_archives","complete","dropped_tables","environment","tables","total_bytes"]':
    "EnvironmentUsageOut",
  '["bytes","count","unmeasured"]': "UsagePartOut",
  '["complete","environment","locations","older_than_days"]': "OrphanedLocationsOut",
  '["bytes","bytes_deleted","deleted","files","files_deleted","kept_because","location","newest_file_at","recent"]':
    "OrphanedLocationOut",
  '["option_keys","root","secret_ref"]': "EnvironmentStorageOut",
  '["alert","band","column","effect","id","last_fired_at","observed","observed_at","signal","snapshot_id","state","values_hidden"]':
    "HealthSignalOut",
  '["alert","band","column","effect","id","observed","observed_at","signal","snapshot_id","state","values_hidden"]':
    "HealthSignalOut",
  '["artifact","created_at","expires_at","upload_id"]': "ArtifactUploadOut",
  '["asset","attempt","checks","failure","finished_at","kind","max_attempts","rows_written","started_at","status","waiting_for"]':
    "TaskRunOut",
  '["at","column","id","table","target","target_column","target_exists"]': "ModelReferenceOut",
  '["at","count","environment","kind","ref","since","title"]': "AttentionItemOut",
  '["asset","repository"]': "ConnectionOut.declared_by",
  '["attempt","bytes_read","cpu_seconds","error_message","failure","finished_at","outcome","peak_memory_bytes","rows_written","started_at","worker"]':
    "TaskAttemptOut",
  '["attempts","complete","more","next_offset","reset","text"]': "TaskLogOut",
  '["audience","complete","hidden","local","steps"]': "GetStartedOut",
  '["auth"]': "-",
  '["base_url"]': "-",
  '["backup_eligible","created_at","disabled_reason","id","last_used_at","name","usable_here"]':
    "PasskeyOut",
  '["binary"]': "-",
  '["bucket","endpoint","region"]': "-",
  '["bound","column","created_at","created_by","environment","expression","group","group_id","id","namespace","principal","principal_id","table"]':
    "ColumnMaskOut",
  '["bound","created_at","created_by","environment","expression","group","group_id","id","namespace","principal","principal_id","table"]':
    "PolicyOut",
  '["browse_url","bytes","commit","content","defined","deployed_at","line","message","path","project_version_id","reason","repository"]':
    "TableDefinitionFileOut",
  '["browse_url","commit","defined","deployed_at","line","path","project_version_id","repository"]':
    "TableDefinitionOut",
  '["capped","items","next_cursor"]': "TableListPageOut",
  '["cause","code","next_step"]': "FailureOut",
  '["causes","detail","state","workers_alive","workers_total"]': "InstanceStatusOut",
  '["check","error","failing_rows","finished_at","kept","name","not_published","on_failure","outcome","rollback_outcome","run_id"]':
    "CheckHealthOut",
  '["checks","days","hidden_checks","last_maintenance","readers","signals"]': "TableHealthOut",
  '["checks","grain","hidden_references","key","key_none","kind","measures","references"]':
    "TableModelOut",
  '["claimed_by","kind","name","size"]': "RawEntryOut",
  '["code","message"]': "WaitingOut",
  '["column","column_type","kind","language","matched","snippet","translated"]': "TableMatchOut",
  '["column","table"]': "MaskedColumnOut",
  '["columns","columns_cut","duration_ms","masked_columns","masked_columns_cut","next_offset","preview","row_count","row_cut","rows"]':
    "ResultPageOut",
  '["created_at","finished_at","id","message","outcome","requested_by","state"]':
    "ConnectionLastTestOut",
  '["connection_kind","distance","id","kind","name"]': "GraphNodeOut",
  '["count","environments"]': "TableTreeOut",
  '["created_at","display_name","expires_at","id","name"]': "OpenInviteOut",
  '["created_at","environment","health_summary","kind","last_read_at","last_write_at","match","model_kind","name","namespace","owner","readers_7d","row_count","size_bytes","state","uuid"]':
    "TableListItemOut",
  '["created_at","expires_at","id","name"]': "OpenRecoveryLinkOut",
  '["created_at","finished_at","host_keys","id","kind","message","outcome","state"]':
    "RepositoryProbeOut",
  '["created_at","id","kind","subject"]': "NoticeOut",
  '["database","host","port"]': "-",
  '["cron","kind","next_run_at","paused","timezone"]': "ScheduleTriggerOut",
  '["days","expires_at","kept","last_activity_at","name","namespace","never","on","set_at","set_by","source","uuid"]':
    "TableRetentionOut",
  '["deleted_at","is_protected","name","properties","purge_after","retention_days","storage_root","storage_secret_ref"]':
    "EnvironmentOut",
  '["description","description_language","description_source","label","label_language","label_source","name","nullable","terms","type"]':
    "ColumnOut",
  '["detail","type"]': "ErrorBody",
  '["detail"]': "ErrorBody",
  '["display_name","kind","name","state"]': "LinkDescriptionOut",
  '["distance","id","kind","name","repository"]': "GraphNodeOut",
  '["distance","id","kind","name","table_type"]': "GraphNodeOut",
  '["distance","id","kind","name"]': "GraphNodeOut",
  '["done","id"]': "GetStartedStepOut",
  '["edges","next_cursor","nodes"]': "GraphOut",
  '["environment","fingerprint","made","public_key"]': "DeployKeyMadeOut",
  '["environment","job","last_run_at","last_run_status","repository"]': "WorkJobOut",
  '["environment","kind","namespace","table"]': "ScopeOut",
  '["environment","kind","opened_at","ref"]': "RecentOut",
  '["environment","last_deploy_at","last_deploy_status"]': "WorkEnvironmentOut",
  '["environments","name"]': "WorkRepositoryOut",
  '["finished_at","outcome","state"]': "ConnectionTestOut",
  '["events","format","secret"]': "NotifyOut",
  '["failing_rows","name","outcome","table"]': "CheckResultOut",
  '["failure","run","task"]': "FailedRunOut",
  '["fingerprint","line","type"]': "HostKeyOut",
  '["get_started_hidden","tour_seen","update_note_seen"]': "InterfaceStateOut",
  '["grant_id","group","kind","role","scope"]': "ViaOut",
  '["has_value","name","secret","source","value"]': "ConfigurationSettingOut",
  '["hidden","next_cursor","orphans"]': "-",
  '["hidden_incoming","hidden_outgoing","incoming","outgoing","table"]': "TableReferencesOut",
  '["high","low","max","min"]': "HealthBandOut",
  '["high","low","median"]': "HealthBandOut",
  '["high"]': "HealthBandOut",
  '["id","name","principal_type"]': "SubjectPrincipalOut",
  '["is_active","name","picture","type"]': "PrincipalOut",
  '["is_protected","name"]': "EnvironmentCreate",
  '["item","others","state","text"]': "HealthSummaryOut",
  '["items","next_cursor","path"]': "RawPageOut",
  '["items","next_cursor"]': "WirePage",
  '["items","truncated"]': "AttentionOut",
  '["job","last_commit_at","seconds","source"]': "HealthObservedOut",
  '["jobs","repositories"]': "WorkOut",
  '["keys","notes","replaced","secret","skipped"]': "DeployKeyOut",
  '["kind","lower","upper"]': "HealthObservedOut",
  '["kind","name"]': "OwnerOut",
  '["kind","source","target"]': "GraphEdgeOut",
  '["kind"]': "DeployTriggerOut",
  '["landing","raw"]': "-",
  '["last_read_at","principal","read_count"]': "ReaderOut",
  '["memory_limit_bytes","memory_used_bytes","slots_busy","slots_total","workers"]':
    "StatusDetailOut",
  '["name","type"]': "ResultColumnOut",
  // A group (GroupOut) and an owner without its kind: the name is free text in both.
  '["name"]': "GroupOut",
  '["nulls","rate","values"]': "HealthObservedOut",
  '["path","root"]': "NamespaceOut",
  '["path","properties"]': "NamespaceOut",
  '["path","repository"]': "DefinedByOut",
  '["path","sha256","size"]': "ArtifactFileOut",
  '["query_id","status"]': "QueryStatusOut",
  '["query_id"]': "QueryStatusOut",
  // A preview's `{table}`: the one type whose fields it fills exactly is the claim of an entry of
  // raw/ (#672); `table` is free text in both, so it carries the override either way.
  '["table"]': "RawEntryOut.claimed_by",
  '["tls","user"]': "-",
  '["version"]': "InfoOut",
  "[]": "-",
  // An environment's properties (Settings › Environments): keys and values are data.
  '["cost-centre"]': "-",
  '["actions","commit_subject","created_at","environment","error_message","finished_at","id","jobs","kind","project_version_id","repository","requested_ref","resolved_sha","started_at","status","submitted_by"]':
    "DeployOut",
  '["commit_sha","project_version_id","repository","updated_at"]': "DeploymentOut",
  '["id","status"]': "DeployAcceptedOut",
  '["downstream","patterns","upstream"]': "JobOut",
  // Access's places: a held grant, a managed scope, a group's member and the access files' status.
  '["action","environment","granted_at","group","id","kind","namespace","note","role","table"]':
    "HeldGrantOut",
  '["applied_at","applied_by","file_name","file_sha256","scope","source_commit"]':
    "ManagedScopeOut",
  '["applied_at","file_name","scope"]': "ManagedScopeBriefOut",
  '["at","by","change","reason","scope"]': "AccessOverrideOut",
  '["held_admin","is_active","name","picture","type"]': "PrincipalOut",
  '["overrides","scopes"]': "AccessStatusOut",
  '["asset","asset_scope","environment","expires_at","issued_at","job","key_id","principal","run_id"]':
    "TaskTokenOut",
  '["created_at","created_by","environment","format_version","id","key_id","name","rotated_at","use","use_target","use_target_url"]':
    "SecretOut",
  '["created_at","expires_at","key_id","last_used_at","name","owner","revoked_at"]': "TokenOut",
};

/**
 * An object's keys as {@link READ_AS} holds them: sorted, as JSON, as a key that comes from data
 * (in `observed` or `properties`) may hold any character.
 *
 * @param keys - The object's keys.
 * @returns The key.
 */
export function readKey(keys: readonly string[]): string {
  return JSON.stringify([...keys].sort());
}

/** The objects `poison` read since {@link takeReads} last ran: their keys, and the type. */
const reads = new Map<string, string>();

/**
 * The objects `poison` read since the last call, each with the type it read it as; then forgets
 * them.
 *
 * @returns The type's name by {@link readKey}, `-` for none.
 */
export function takeReads(): ReadonlyMap<string, string> {
  const taken = new Map(reads);
  reads.clear();
  return taken;
}

/**
 * The fields of an answer's object that hold codes: those of its type ({@link typeOf}) whose
 * type is a union of string literals, and its {@link STRING_CODES}. Records the pick for
 * {@link takeReads}.
 *
 * @param keys - The object's keys.
 * @returns The fields.
 */
function codeFields(keys: readonly string[]): ReadonlySet<string> {
  const type = typeOf(API_TYPES, keys);
  reads.set(readKey(keys), type?.name ?? "-");
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
  // The scope is chosen once: wait until the interface has mounted one.
  await expect(page.locator("main, [role=dialog]").first()).toBeAttached();
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
