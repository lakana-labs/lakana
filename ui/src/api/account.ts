/**
 * A person's passkeys and browser sessions: their own, or, for an administrator, anyone's.
 *
 * @remarks
 * Removing a passkey and ending another person's sessions need a fresh passkey touch;
 * {@link api}.request asks for it and sends the request once more.
 * @packageDocumentation
 */
import { api } from "./client";

/** One passkey, as `GET /api/v1/principals/{name}/passkeys` lists it; never its key. */
export interface PasskeyOut {
  /** The passkey's id, to remove it by. */
  readonly id: string;
  /** What its person called it; may be blank. */
  readonly name: string;
  /** When it was added. */
  readonly created_at: string;
  /** When it last signed in or confirmed an action, or null for never. */
  readonly last_used_at: string | null;
  /** True for a synced passkey, which may be on several devices. */
  readonly backup_eligible: boolean;
  /** `possible_clone` when it was disabled; null while it works. */
  readonly disabled_reason: string | null;
  /** False when it was registered for another public address of this server. */
  readonly usable_here: boolean;
}

/** One open browser session, as `GET /api/v1/principals/{name}/sessions` lists it. */
export interface BrowserSessionOut {
  /** The session's id. */
  readonly id: string;
  /** How it began: `passkey`, `setup`, `invite`, `recovery` or `local`. */
  readonly via: string;
  /** When it began. */
  readonly created_at: string;
  /** About when it was last used. */
  readonly last_seen_at: string;
  /** When it ends at the latest. */
  readonly absolute_expires_at: string;
  /** The browser it began in, as the browser named itself; may be blank. */
  readonly user_agent: string;
  /**
   * The same in plain words for the common browsers ("Firefox 158 on Ubuntu"), otherwise the
   * user agent itself; blank when the browser sent none.
   */
  readonly browser: string;
  /** The address it began from. */
  readonly peer: string;
  /** True for the session of the request that asks: this browser. */
  readonly current: boolean;
}

/**
 * The path of a principal's route.
 *
 * @param name - The principal name.
 * @param rest - The rest of the path, starting with `/`.
 * @returns The path.
 */
function principalPath(name: string, rest: string): string {
  return `/api/v1/principals/${encodeURIComponent(name)}${rest}`;
}

/**
 * Lists a person's passkeys.
 *
 * @param name - The person's principal name.
 * @returns The passkeys.
 */
export async function listPasskeys(name: string): Promise<readonly PasskeyOut[]> {
  return api.request<readonly PasskeyOut[]>("GET", principalPath(name, "/passkeys"));
}

/**
 * Removes a passkey; needs a passkey touch.
 *
 * @param name - The person's principal name.
 * @param id - The passkey's id.
 */
export async function removePasskey(name: string, id: string): Promise<void> {
  await api.request("DELETE", principalPath(name, `/passkeys/${encodeURIComponent(id)}`));
}

/**
 * Lists a person's open browser sessions.
 *
 * @param name - The person's principal name.
 * @returns The sessions, oldest first.
 */
export async function listSessions(name: string): Promise<readonly BrowserSessionOut[]> {
  return api.request<readonly BrowserSessionOut[]>("GET", principalPath(name, "/sessions"));
}

/**
 * Ends every browser session of another person; needs a passkey touch.
 *
 * @param name - The person's principal name.
 */
export async function endSessions(name: string): Promise<void> {
  await api.request("DELETE", principalPath(name, "/sessions"));
}
