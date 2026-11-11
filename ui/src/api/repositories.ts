/**
 * Repositories as the Connect a repository page uses them: the checks a worker runs (the host's
 * SSH keys, access with a deploy key), registering an SSH repository and making its keys.
 *
 * @remarks
 * No answer and no request here carries a private key: the server keeps each environment's
 * private key as a secret and answers only the public halves.
 * @packageDocumentation
 */
import { api } from "./client";

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
  /** `host_keys` or `access`. */
  readonly kind: string;
  /** `queued`, `running` or `done`. */
  readonly state: string;
  /** What it found once done (`ok`, `no_worker`, `auth_refused`, ...); null before. */
  readonly outcome: string | null;
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
}

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
