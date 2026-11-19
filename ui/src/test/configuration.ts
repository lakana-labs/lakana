/**
 * Fixtures of Settings › Configuration: the running server's settings as
 * `GET /api/v1/settings/configuration` answers them.
 *
 * @remarks
 * Written as literals: no test reads a file outside `ui/`. `CONFIGURATION_FULL` holds every
 * setting the server describes, in its order, with the defaults of a server whose database URL
 * and master key are set; `CONFIGURATION_SAMPLE` a dozen across three groups.
 * @packageDocumentation
 */
import type { ConfigurationSettingOut } from "../api/configuration";

/**
 * A setting.
 *
 * @param name - The environment variable.
 * @param value - Its value in JSON form; null for a secret or a setting without one.
 * @param more - What differs from a plain setting left at its default.
 * @returns The setting.
 */
export function aSetting(
  name: string,
  value: ConfigurationSettingOut["value"],
  more: Partial<Omit<ConfigurationSettingOut, "name" | "value">> = {},
): ConfigurationSettingOut {
  return { name, value, secret: false, has_value: value !== null, source: "default", ...more };
}

/**
 * A secret, set by the environment, or not set; the server sends no value for it.
 *
 * @param name - The environment variable.
 * @param set - Whether it is set.
 * @returns The setting.
 */
function aSecret(name: string, set: boolean): ConfigurationSettingOut {
  return aSetting(name, null, { secret: true, has_value: set, source: set ? "set" : "default" });
}

/** A dozen settings across three groups: one secret set and one not, a list and an object. */
export const CONFIGURATION_SAMPLE: readonly ConfigurationSettingOut[] = [
  aSecret("LAKANA_DATABASE__URL", true),
  aSetting("LAKANA_DATABASE__ECHO", false),
  aSetting("LAKANA_BOOTSTRAP_ADMIN", "admin", { source: "set" }),
  aSecret("LAKANA_SECRETS__MASTER_KEY", true),
  aSetting("LAKANA_RUN__LEASE_SECONDS", 60),
  aSetting("LAKANA_RUN__VENV_MAX_BYTES", 10737418240),
  aSecret("LAKANA_RUN__HTTPS_PROXY", false),
  aSetting("LAKANA_RUN__CA_BUNDLE", null),
  aSetting("LAKANA_RUN__INGESTION_ROOTS", { raw: "/srv/raw" }, { source: "set" }),
  aSetting("LAKANA_CI_OIDC__ALGORITHMS", ["RS256"]),
  aSetting("LAKANA_HOST", "0.0.0.0"),
  aSetting("LAKANA_PORT", 8000, { source: "set" }),
];

/** Every setting the server describes, in its order. */
export const CONFIGURATION_FULL: readonly ConfigurationSettingOut[] = [
  aSecret("LAKANA_DATABASE__URL", true),
  aSetting("LAKANA_DATABASE__ECHO", false),
  aSetting("LAKANA_BOOTSTRAP_ADMIN", "admin"),
  aSetting("LAKANA_BOOTSTRAP_PAT_KEY_ID", null),
  aSecret("LAKANA_BOOTSTRAP_PAT_SECRET", false),
  aSetting("LAKANA_BOOTSTRAP_TOKEN_FILE", null),
  aSecret("LAKANA_SECRETS__MASTER_KEY", true),
  aSetting("LAKANA_SECRETS__ACCEPT_FORMAT_1", false),
  aSetting("LAKANA_TASK_TOKENS__MAX_TTL_SECONDS", 86400),
  aSetting("LAKANA_ARTIFACTS__MAX_FILE_BYTES", 2147483648),
  aSetting("LAKANA_ARTIFACTS__MAX_BYTES", 10737418240),
  aSetting("LAKANA_ARTIFACTS__MAX_FILES", 10000),
  aSetting("LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS", 3600),
  aSetting("LAKANA_TABLES__UNDO_DAYS", 7),
  aSetting("LAKANA_TABLES__EXPIRY_WARNING_DAYS", 7),
  aSetting("LAKANA_TABLE_READS__KEEP_DAYS", 90),
  aSetting("LAKANA_TABLE_READS__DEFAULT_DAYS", 30),
  aSetting("LAKANA_HEALTH__RETENTION_DAYS", 90),
  aSetting("LAKANA_APPEND__MAX_BYTES", 268435456),
  aSetting("LAKANA_APPEND__MAX_CONCURRENT", 4),
  aSetting("LAKANA_APPEND__MAX_CONCURRENT_HISTORY", 1),
  aSetting("LAKANA_APPEND__READ_TIMEOUT_SECONDS", 300.0),
  aSetting("LAKANA_APPEND__MAX_ARRAYS", 100000),
  aSetting("LAKANA_REQUEST_BODY__MAX_BYTES", 1048576),
  aSetting("LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE", 10),
  aSetting("LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE", 30),
  aSetting("LAKANA_VIEW_AUDIT__ENABLED", true),
  aSetting("LAKANA_VIEW_AUDIT__WINDOW_MINUTES", 60),
  aSetting("LAKANA_AUDIT__QUERY_TEXT", "full"),
  aSetting("LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES", 15),
  aSetting("LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS", 1000000),
  aSetting("LAKANA_QUERY_LIMITS__MAX_RESULT_BYTES", 268435456),
  aSetting("LAKANA_QUERY_LIMITS__MEMORY_LIMIT", "1GB"),
  aSetting("LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS", 100000000),
  aSetting("LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS", 900.0),
  aSetting("LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS", 10.0),
  aSetting("LAKANA_QUERY_LIMITS__THREADS", 4),
  aSetting("LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE", "10GB"),
  aSetting("LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS", 100000),
  aSetting("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", "auto"),
  aSetting("LAKANA_RUN__MAX_RETRIES", 5),
  aSetting("LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS", 1.0),
  aSetting("LAKANA_RUN__LEASE_SECONDS", 60),
  aSetting("LAKANA_RUN__RENEW_SECONDS", 20),
  aSetting("LAKANA_RUN__DRAIN_GRACE_SECONDS", 20.0),
  aSetting("LAKANA_RUN__SERVER_URL", null),
  aSetting("LAKANA_RUN__VENV_CACHE", null),
  aSetting("LAKANA_RUN__VENV_KEEP", 10),
  aSetting("LAKANA_RUN__VENV_BUILD_TIMEOUT", 600.0),
  aSetting("LAKANA_RUN__VENV_MAX_BYTES", 5368709120),
  aSetting("LAKANA_RUN__KILL_GRACE", 10.0),
  aSetting("LAKANA_RUN__SWEEP_INTERVAL", 3600.0),
  aSetting("LAKANA_RUN__MAINTENANCE_TIMEOUT", 1800.0),
  aSetting("LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS", 1000000),
  aSetting("LAKANA_RUN__MAX_HISTORY_OPEN_BYTES", 536870912),
  aSecret("LAKANA_RUN__INDEX_URL", false),
  aSecret("LAKANA_RUN__HTTPS_PROXY", false),
  aSetting("LAKANA_RUN__NO_PROXY", null),
  aSetting("LAKANA_RUN__CA_BUNDLE", null),
  aSetting("LAKANA_RUN__RUNNER", "local"),
  aSetting("LAKANA_RUN__LAUNCHER_SOCKET", "/run/lakana-launcher/launcher.sock"),
  aSetting("LAKANA_RUN__TASK_MEMORY", "2GB"),
  aSetting("LAKANA_RUN__TASK_CPUS", 1.0),
  aSetting("LAKANA_RUN__TASK_PIDS", 256),
  aSetting("LAKANA_RUN__INGESTION_ROOTS", {}),
  aSetting("LAKANA_LOG__LEVEL", "INFO"),
  aSetting("LAKANA_LOG__ACCESS", true),
  aSetting("LAKANA_CI_OIDC__ISSUERS", []),
  aSetting("LAKANA_CI_OIDC__AUDIENCE", null),
  aSetting("LAKANA_CI_OIDC__ALGORITHMS", ["RS256"]),
  aSetting("LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS", 900),
  aSetting("LAKANA_CI_OIDC__ALLOW_HOSTS", []),
  aSecret("LAKANA_CI_OIDC__PROXY", false),
  aSetting("LAKANA_CI_OIDC__CA_BUNDLE", null),
  aSetting("LAKANA_CI_OIDC__TOKEN_TTL_SECONDS", 3600),
  aSetting("LAKANA_READINESS__TIMEOUT_SECONDS", 2.0),
  aSetting("LAKANA_READINESS__CACHE_SECONDS", 5.0),
  aSetting("LAKANA_READINESS__ENVIRONMENTS", []),
  aSetting("LAKANA_UI__ENABLED", false),
  aSetting("LAKANA_UI__PUBLIC_URL", null),
  aSetting("LAKANA_UI__ALLOWED_HOSTS", []),
  aSetting("LAKANA_UI__LOCAL_SIGN_IN", false),
  aSetting("LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE", 60),
  aSetting("LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE", 240),
  aSetting("LAKANA_HOST", "127.0.0.1"),
  aSetting("LAKANA_PORT", 8000),
  aSetting("LAKANA_TRUSTED_PROXIES", []),
  aSetting("LAKANA_REPO_ROOTS", []),
  aSetting("LAKANA_MAIN_LANGUAGE", "en"),
];
