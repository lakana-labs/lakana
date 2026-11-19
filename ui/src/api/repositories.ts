/**
 * Repositories: the checks a worker runs (the host's SSH keys, access with a deploy key),
 * registering an SSH repository and making its keys, as Connect a repository uses them; and the
 * list, a repository's page, its edit, its deploy keys and its removal, as Settings uses them.
 *
 * @remarks
 * No answer and no request here carries a private key: the server keeps each environment's
 * private key as a secret and answers only the public halves.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";

/** How often a check is asked about while a worker runs it. */
const POLL_MS = 1000;

/** One SSH host key a check found. */
export interface HostKeyOut {
  /** The key type, such as `ssh-ed25519`. */
  readonly type: string;
  /** `SHA256:…`, to compare with the host's published fingerprints. */
  readonly fingerprint: string;
  /** The known_hosts line for the repository. */
  readonly line: string;
}

/** A check a worker runs, as `GET /api/v1/repository-probes/{id}` answers it. */
export interface RepositoryProbeOut {
  /** The check's id. */
  readonly id: string;
  /** What it checks. The server may add values: compare, and keep a fallback. */
  readonly kind: "host_keys" | "access";
  /** Where it is. The server may add values: compare, and keep a fallback. */
  readonly state: "queued" | "running" | "done";
  /** What it found once done; null before. The server may add values: compare, and keep a fallback. */
  readonly outcome:
    | "ok"
    | "host_key_changed"
    | "host_key_unknown"
    | "auth_refused"
    | "not_found"
    | "unreachable"
    | "refused_address"
    | "timeout"
    | "no_worker"
    | "failed"
    | null;
  /** What it found, with the next step; null before it is done. */
  readonly message: string | null;
  /** The keys a host-key check found; null otherwise. */
  readonly host_keys: readonly HostKeyOut[] | null;
}

/** A check to queue: the host's keys of a URL, or access to a registered repository. */
export type RepositoryProbeIn =
  | { readonly kind: "host_keys"; readonly url: string }
  | { readonly kind: "access"; readonly repository: string; readonly environment: string };

/** An SSH repository to register, with the host keys the person confirmed. */
export interface RepositoryCreate {
  /** Its name. */
  readonly name: string;
  /** Always `ssh` here. */
  readonly kind: "ssh";
  /** Its SSH URL. */
  readonly url: string;
  /** The confirmed known_hosts lines, one per line. */
  readonly ssh_known_hosts: string;
}

/** One environment's public deploy key. */
interface DeployKeyMadeOut {
  /** The environment whose secret holds the private half. */
  readonly environment: string;
  /** `ssh-ed25519 <base64> <comment>`, to add at the git host. */
  readonly public_key: string;
  /** `SHA256:…`. */
  readonly fingerprint: string;
  /** Whether this call made it; false for a key made earlier. */
  readonly made: boolean;
}

/** The answer of `POST /api/v1/repositories/{name}/deploy-key`. */
export interface DeployKeyOut {
  /** The secret every key is stored under. */
  readonly secret: string;
  /** One key per environment the call wrote or found one in. */
  readonly keys: readonly DeployKeyMadeOut[];
  /** Environments without a key where the person may not manage secrets. */
  readonly skipped: readonly string[];
  /** Environments whose key this call replaced. */
  readonly replaced: readonly string[];
  /** For each replaced key: the old key stops working, replace it at the git host now. */
  readonly notes: readonly string[];
}

/** One environment's public deploy key, as a repository lists it. */
export interface RepositoryDeployKeyOut {
  /** The environment whose secret holds the private half. */
  readonly environment: string;
  /** `ssh-ed25519 <base64> <comment>`, to add at the git host. */
  readonly public_key: string;
  /** `SHA256:…`. */
  readonly fingerprint: string;
}

/** A host key a repository trusts, by type and fingerprint. */
export interface TrustedHostKeyOut {
  /** The key type, such as `ssh-ed25519`. */
  readonly type: string;
  /** `SHA256:…`, to compare with the host's published fingerprints. */
  readonly fingerprint: string;
}

/** A registered repository, as `GET /api/v1/repositories/{name}` answers it. */
export interface RepositoryOut {
  /** Its id. */
  readonly id: string;
  /** Its name. */
  readonly name: string;
  /** Where it is read from. */
  readonly kind: "local" | "https" | "ssh";
  /** Its URL, or a folder on the server for `local`. */
  readonly url: string;
  /** The secret holding its credential, by name; null without one. */
  readonly credential_secret: string | null;
  /** The HTTPS username; null for `oauth2`. */
  readonly credential_username: string | null;
  /** The host keys it trusts, as known_hosts lines; null without any. */
  readonly ssh_known_hosts: string | null;
  /** The ref deployed when none is given; null for the remote's default. */
  readonly default_ref: string | null;
  /** When it was registered. */
  readonly created_at: string;
  /** When it last changed. */
  readonly updated_at: string;
  /** The public deploy keys of the environments the person may read. */
  readonly deploy_keys: readonly RepositoryDeployKeyOut[];
  /** The host keys `ssh_known_hosts` trusts, each with its fingerprint; absent from an older server. */
  readonly host_keys?: readonly TrustedHostKeyOut[];
}

/** What Connect reads of a registered repository. */
type RegisteredRepository = Pick<RepositoryOut, "name" | "kind" | "url" | "ssh_known_hosts">;

/** The fields an edit may change; null clears an optional one. */
export type RepositoryChanges = Partial<
  Record<
    "url" | "credential_secret" | "credential_username" | "ssh_known_hosts" | "default_ref",
    string | null
  >
>;

/**
 * Queues a check for a worker to run.
 *
 * @param body - The check.
 * @returns The queued check; poll it with {@link waitForProbe}.
 */
export async function requestProbe(body: RepositoryProbeIn): Promise<RepositoryProbeOut> {
  return api.request<RepositoryProbeOut>("POST", "/api/v1/repository-probes", body);
}

/**
 * Waits one poll interval, or until the wait is given up.
 *
 * @param signal - Aborted when the page no longer waits.
 * @returns When the interval is over.
 * @throws {Error} when the wait was given up.
 */
async function pause(signal: AbortSignal): Promise<void> {
  const stopped = new Error("The wait for the check was stopped.");
  if (signal.aborted) {
    throw stopped;
  }
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stop);
      resolve();
    }, POLL_MS);
    function stop(): void {
      clearTimeout(timer);
      reject(stopped);
    }
    signal.addEventListener("abort", stop, { once: true });
  });
}

/**
 * Asks about a check every second, in the background, until a worker has done it.
 *
 * @param id - The check's id.
 * @param signal - Aborted when the page no longer waits: the polls stop.
 * @returns The check, done.
 * @throws {Error} when the wait was given up, or the server's refusal.
 */
export async function waitForProbe(id: string, signal: AbortSignal): Promise<RepositoryProbeOut> {
  for (;;) {
    await pause(signal);
    const probe = await api.request<RepositoryProbeOut>(
      "GET",
      `/api/v1/repository-probes/${encodeURIComponent(id)}`,
      undefined,
      { background: true },
    );
    if (probe.state === "done") {
      return probe;
    }
  }
}

/**
 * Registers an SSH repository; it asks for a passkey touch when the last one is old.
 *
 * @param body - The repository.
 * @returns When it is registered.
 */
export async function createRepository(body: RepositoryCreate): Promise<void> {
  await api.request("POST", "/api/v1/repositories", body);
}

/**
 * Reads a registered repository, to compare it with the one the page registers.
 *
 * @param name - Its name.
 * @returns Its name, kind, URL and trusted host keys.
 */
export async function getRepository(name: string): Promise<RegisteredRepository> {
  const repository = await api.request<RepositoryOut>(
    "GET",
    `/api/v1/repositories/${encodeURIComponent(name)}`,
  );
  return {
    name: repository.name,
    kind: repository.kind,
    url: repository.url,
    ssh_known_hosts: repository.ssh_known_hosts,
  };
}

/**
 * Makes a deploy key in every environment where the person may manage secrets and the
 * repository has none yet; the private keys stay on the server.
 *
 * @param name - The repository's name.
 * @returns The public keys, by environment, and the environments skipped.
 */
export async function generateDeployKey(name: string): Promise<DeployKeyOut> {
  return api.request<DeployKeyOut>(
    "POST",
    `/api/v1/repositories/${encodeURIComponent(name)}/deploy-key`,
    {},
  );
}

/**
 * The path of a repository.
 *
 * @param name - Its name, encoded here.
 * @returns The path.
 */
const repositoryPath = (name: string): string => `/api/v1/repositories/${encodeURIComponent(name)}`;

/**
 * Lists the repositories, by name; the server answers them all at once.
 *
 * @returns The repositories.
 */
export async function listRepositories(): Promise<readonly RepositoryOut[]> {
  return api.request<readonly RepositoryOut[]>("GET", "/api/v1/repositories");
}

/**
 * Reads one repository whole, for its page.
 *
 * @param name - Its name.
 * @returns The repository.
 */
export async function getRepositoryPage(name: string): Promise<RepositoryOut> {
  return api.request<RepositoryOut>("GET", repositoryPath(name));
}

/**
 * Changes a repository; the passkey is asked for first when the last touch is old.
 *
 * @param name - Its name.
 * @param changes - Only the fields that change; null clears an optional one.
 * @returns The repository as changed.
 */
export async function updateRepository(
  name: string,
  changes: RepositoryChanges,
): Promise<RepositoryOut> {
  return guarded<RepositoryOut>("PATCH", repositoryPath(name), changes);
}

/**
 * Removes a repository; the server refuses while an environment still deploys it.
 *
 * @param name - Its name.
 * @returns When it is removed.
 */
export async function deleteRepository(name: string): Promise<void> {
  await api.request("DELETE", repositoryPath(name));
}

/**
 * Replaces one environment's deploy key, after the passkey; the old key stops working at once.
 *
 * @param name - The repository's name.
 * @param environment - The environment whose key is replaced.
 * @returns The new public key, and the note to replace it at the git host.
 */
export async function replaceDeployKey(name: string, environment: string): Promise<DeployKeyOut> {
  return guarded<DeployKeyOut>("POST", `${repositoryPath(name)}/deploy-key`, {
    environments: [environment],
    replace: true,
  });
}

/**
 * Makes the missing deploy keys, after the passkey: one in every environment where the person
 * may manage secrets and the repository has none yet.
 *
 * @param name - The repository's name.
 * @returns The public keys, by environment, and the environments skipped.
 */
export async function makeMissingDeployKeys(name: string): Promise<DeployKeyOut> {
  return guarded<DeployKeyOut>("POST", `${repositoryPath(name)}/deploy-key`, {});
}
