/**
 * Who may sign in: invites, recovery links, and the people an administrator looks after.
 *
 * @remarks
 * Issuing an invite or a recovery link needs a fresh passkey touch, so those writes live in
 * `signInAdmin.ts`, which asks for it first. A new link is in the answer that issues it and
 * nowhere else: the interface shows it once and never keeps it.
 * @packageDocumentation
 */
import { api } from "./client";

/** A new invite, as `POST /api/v1/invites` answers it; its link is only here. */
export interface InviteOut {
  /** The invite's id, to withdraw it by. */
  readonly id: string;
  /** The principal name the invite creates. */
  readonly name: string;
  /** The display name, if one was given. */
  readonly display_name: string | null;
  /** The link to send: it works once, for 7 days. */
  readonly url: string;
  /** When the link stops working. */
  readonly expires_at: string;
}

/** An open invite, as `GET /api/v1/invites` lists it; without its link. */
export interface OpenInviteOut {
  /** The invite's id. */
  readonly id: string;
  /** The principal name it creates. */
  readonly name: string;
  /** The display name, if one was given. */
  readonly display_name: string | null;
  /** When it was issued. */
  readonly created_at: string;
  /** When it stops working. */
  readonly expires_at: string;
}

/** A new recovery link, as the route that issues it answers; its link is only here. */
export interface RecoveryLinkOut {
  /** The link's id, to withdraw it by. */
  readonly id: string;
  /** The person it recovers. */
  readonly name: string;
  /** The link to send: it works once, for 1 hour. */
  readonly url: string;
  /** When the link stops working. */
  readonly expires_at: string;
}

/** An open recovery link, as `GET /api/v1/recovery-links` lists it; without the link. */
export interface OpenRecoveryLinkOut {
  /** The link's id. */
  readonly id: string;
  /** The person it recovers. */
  readonly name: string;
  /** When it was issued. */
  readonly created_at: string;
  /** When it stops working. */
  readonly expires_at: string;
}

/** A principal, as `GET /api/v1/principals` lists it. */
export interface PrincipalOut {
  /** The principal name. */
  readonly name: string;
  /** `USER` for a person, `SERVICE` for a service account. */
  readonly type: string;
  /** False for a deactivated principal. */
  readonly is_active: boolean;
  /** The etag of the principal's profile picture, or null without one. */
  readonly picture: string | null;
  /**
   * Whether the principal holds or has ever held the ADMIN role on the instance, which makes the
   * server operator the only one who can recover them; null unless the caller is an
   * administrator. Absent from an older server.
   */
  readonly held_admin?: boolean | null;
}

/** What the permissions route answers, as far as finding an administrator goes. */
interface PermissionsOut {
  /** One entry per action, with the grants that give it. */
  readonly permissions: readonly { readonly via: readonly { readonly role: string | null }[] }[];
}

/** How many people the list shows at most. */
export const PEOPLE_LIMIT = 200;

/**
 * The path of a principal's route.
 *
 * @param name - The principal name.
 * @param rest - The rest of the path, starting with `/`, if any.
 * @returns The path.
 */
function principalPath(name: string, rest = ""): string {
  return `/api/v1/principals/${encodeURIComponent(name)}${rest}`;
}

/**
 * Lists the open invites, oldest first.
 *
 * @returns The invites.
 */
export async function listInvites(): Promise<readonly OpenInviteOut[]> {
  return api.request<readonly OpenInviteOut[]>("GET", "/api/v1/invites");
}

/**
 * Withdraws an open invite; its link stops working.
 *
 * @param id - The invite's id.
 */
export async function withdrawInvite(id: string): Promise<void> {
  await api.request("DELETE", `/api/v1/invites/${encodeURIComponent(id)}`);
}

/**
 * Lists the open recovery links, oldest first.
 *
 * @returns The links.
 */
export async function listRecoveryLinks(): Promise<readonly OpenRecoveryLinkOut[]> {
  return api.request<readonly OpenRecoveryLinkOut[]>("GET", "/api/v1/recovery-links");
}

/**
 * Withdraws an open recovery link; it stops working.
 *
 * @param id - The link's id.
 */
export async function withdrawRecoveryLink(id: string): Promise<void> {
  await api.request("DELETE", `/api/v1/recovery-links/${encodeURIComponent(id)}`);
}

/** The first page of people, and whether the server has more. */
interface PeoplePage {
  /** The principals, newest first. */
  readonly people: readonly PrincipalOut[];
  /** Whether more match than this page holds (the server sent a next cursor). */
  readonly more: boolean;
}

/**
 * Lists the first {@link PEOPLE_LIMIT} people and service accounts, newest first. Whether more
 * exist comes from the server's `X-Next-Cursor`, not from a full page: exactly
 * {@link PEOPLE_LIMIT} people are all of them.
 *
 * @param words - Only those whose name contains this text; blank for all.
 * @returns The principals and whether there are more.
 */
export async function listPeople(words: string): Promise<PeoplePage> {
  const query = new URLSearchParams({ limit: String(PEOPLE_LIMIT) });
  if (words !== "") {
    query.set("q", words);
  }
  const page = await api.page(`/api/v1/principals?${query.toString()}`);
  return { people: page.body as readonly PrincipalOut[], more: page.nextCursor !== null };
}

/**
 * Reads one principal.
 *
 * @param name - The principal name.
 * @returns The principal, with its type.
 */
export async function getPrincipal(name: string): Promise<PrincipalOut> {
  return api.request<PrincipalOut>("GET", principalPath(name));
}

/**
 * Finds out whether a person holds the ADMIN role on the instance, directly or through a group.
 *
 * @param name - The person's principal name.
 * @returns True for an administrator.
 */
export async function isAdministrator(name: string): Promise<boolean> {
  const explained = await api.request<PermissionsOut>("GET", principalPath(name, "/permissions"));
  return explained.permissions.some((permission) =>
    permission.via.some((via) => via.role === "ADMIN"),
  );
}
