/**
 * The browser session: who is signed in, signing out, what the person may use, and their
 * sign-in notices.
 *
 * @packageDocumentation
 */
import { api, setCsrfToken } from "./client";

/** The browser session, as `GET /api/v1/sign-in/session` answers it. */
export interface SessionOut {
  /** The principal name. */
  readonly name: string;
  /** The name to show, when the person has one. */
  readonly display_name: string | null;
  /** The principal type, such as `USER`. */
  readonly type: string;
  /** How the session started: `passkey`, `setup`, `invite`, `recovery` or `local`. */
  readonly via: string;
  /** Until when actions that need a passkey touch need no new one, or null. */
  readonly step_up_until: string | null;
  /** True when the person must add a passkey before anything else. */
  readonly needs_passkey: boolean;
  /** True when the interface may offer to add a passkey. */
  readonly offer_passkey: boolean;
  /** When the session ends at the latest. */
  readonly absolute_expires_at: string;
}

/** What a person may use, as far as the interface's areas go. */
export interface AccessOut {
  /** The person's principal name. */
  readonly name: string;
  /** True for a holder of the ADMIN role on the instance. */
  readonly admin: boolean;
  /** True when the person has no right anywhere yet. */
  readonly noAccessYet: boolean;
  /** True when the person may deploy in at least one environment (Home shows Your work). */
  readonly mayDeploy: boolean;
}

/** A sign-in notice, as `GET /api/v1/sign-in/notices` answers it. */
export interface NoticeOut {
  /** The notice's id. */
  readonly id: string;
  /** What happened: `recovery_used`, `passkey_disabled`, `passkey_added`, ... */
  readonly kind: string;
  /** Whose account it happened to, or null. */
  readonly subject: string | null;
  /** When it happened. */
  readonly created_at: string;
}

/** What `GET /api/v1/whoami` answers about access. */
interface WhoamiOut {
  /** The caller's principal name. */
  readonly name: string;
  /** True for a holder of the ADMIN role on the instance. */
  readonly admin: boolean;
  /** True when the person has any grant at all, at any scope. */
  readonly has_access: boolean;
  /** True when the person may deploy in at least one environment. */
  readonly may_deploy: boolean;
}

/**
 * Reads the browser's session. It never carries the CSRF token: only a sign-in answers that.
 *
 * @returns The session.
 * @throws {ApiError} 401 when the browser is not signed in (no session, or no CSRF token in
 * this origin's storage), with `reason` when a session ended.
 */
export async function getSession(): Promise<SessionOut> {
  return api.request<SessionOut>("GET", "/api/v1/sign-in/session");
}

/** Ends this browser's session and forgets its CSRF token, even when the server failed. */
export async function signOut(): Promise<void> {
  try {
    await api.request("DELETE", "/api/v1/sign-in/session");
  } finally {
    setCsrfToken(null);
  }
}

/**
 * Ends every session of the person, this browser's included.
 *
 * @param name - The person's own principal name.
 */
export async function signOutEverywhere(name: string): Promise<void> {
  await api.request("DELETE", `/api/v1/principals/${encodeURIComponent(name)}/sessions`);
  setCsrfToken(null);
}

/**
 * Finds out what the signed-in person may use: administrators see Access and Settings, a
 * person who may deploy sees Your work on Home, and a person with no grant anywhere is told to
 * ask for one.
 *
 * @returns The access, from `GET /api/v1/whoami`.
 */
export async function myAccess(): Promise<AccessOut> {
  const me = await api.request<WhoamiOut>("GET", "/api/v1/whoami");
  return {
    name: me.name,
    admin: me.admin,
    noAccessYet: !me.has_access,
    mayDeploy: me.may_deploy,
  };
}

/**
 * Lists the person's open sign-in notices; a background request, since nobody asked for it.
 *
 * @returns The notices, newest first.
 */
export async function listNotices(): Promise<readonly NoticeOut[]> {
  return api.request<readonly NoticeOut[]>("GET", "/api/v1/sign-in/notices", undefined, {
    background: true,
  });
}

/**
 * Dismisses one sign-in notice.
 *
 * @param id - The notice's id.
 */
export async function dismissNotice(id: string): Promise<void> {
  await api.request("DELETE", `/api/v1/sign-in/notices/${encodeURIComponent(id)}`);
}
