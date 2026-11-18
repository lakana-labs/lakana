/**
 * Groups and their members, as an administrator looks after them.
 *
 * @remarks
 * Deleting a group and changing its members need a fresh passkey touch; each asks for it before
 * the request (`stepUpFirst`). Creating a group needs none.
 * @packageDocumentation
 */
import type { PrincipalOut } from "./access";
import { api } from "./client";
import type { CursorPage } from "./manage";

/** A group, as `GET /api/v1/groups` lists it. */
export interface GroupOut {
  /** The group's name. */
  readonly name: string;
}

/** What a groups listing asks for. */
export interface GroupQuery {
  /** Only groups whose name contains these words; empty for every group. */
  readonly words: string;
  /** Only the groups this principal belongs to; null for any. */
  readonly member: string | null;
}

/** How many groups a page of the list holds. */
export const GROUPS_PAGE = 50;

/**
 * The API path of a group or one of its parts.
 *
 * @param name - The group's name.
 * @param suffix - What follows the name, such as `/members`.
 * @returns The path.
 */
function groupApiPath(name: string, suffix = ""): string {
  return `/api/v1/groups/${encodeURIComponent(name)}${suffix}`;
}

/**
 * Lists one page of groups, newest first.
 *
 * @param query - The words and the member.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal, such as 404 for an unknown member.
 */
export async function listGroups(
  query: GroupQuery,
  cursor: string | null,
): Promise<CursorPage<GroupOut>> {
  const params = new URLSearchParams();
  if (query.words.trim() !== "") {
    params.set("q", query.words.trim());
  }
  if (query.member !== null) {
    params.set("member", query.member);
  }
  params.set("limit", String(GROUPS_PAGE));
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  const page = await api.page(`/api/v1/groups?${params.toString()}`);
  return { items: page.body as readonly GroupOut[], nextCursor: page.nextCursor };
}

/**
 * Creates a group.
 *
 * @param name - Its name.
 * @returns The group.
 * @throws {ApiError} for a refusal, such as 409 for a name already taken.
 */
export function createGroup(name: string): Promise<GroupOut> {
  return api.request<GroupOut>("POST", "/api/v1/groups", { name });
}

/**
 * Deletes a group, after the passkey touch.
 *
 * @param name - The group's name.
 * @throws {ApiError} for a refusal, such as 409 while grants still name it.
 */
export async function deleteGroup(name: string): Promise<void> {
  await api.request("DELETE", groupApiPath(name), undefined, { stepUpFirst: true });
}

/**
 * Lists a group's members, by name.
 *
 * @param name - The group's name.
 * @returns The members.
 * @throws {ApiError} for a refusal, such as 404 for an unknown group.
 */
export function listMembers(name: string): Promise<readonly PrincipalOut[]> {
  return api.request<readonly PrincipalOut[]>("GET", groupApiPath(name, "/members"));
}

/**
 * Adds a person or service account to a group, after the passkey touch.
 *
 * @param group - The group's name.
 * @param principal - The principal's name.
 * @throws {ApiError} for a refusal, such as 404 for an unknown principal.
 */
export async function addMember(group: string, principal: string): Promise<void> {
  await api.request(
    "PUT",
    groupApiPath(group, `/members/${encodeURIComponent(principal)}`),
    undefined,
    { stepUpFirst: true },
  );
}

/**
 * Removes a member from a group, after the passkey touch.
 *
 * @param group - The group's name.
 * @param principal - The member's name.
 * @throws {ApiError} for a refusal, such as 409 for the last administrator.
 */
export async function removeMember(group: string, principal: string): Promise<void> {
  await api.request(
    "DELETE",
    groupApiPath(group, `/members/${encodeURIComponent(principal)}`),
    undefined,
    { stepUpFirst: true },
  );
}
