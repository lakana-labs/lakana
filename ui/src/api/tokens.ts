/**
 * Personal tokens and task tokens: one's own, and everyone's for an administrator.
 *
 * @remarks
 * Creating a personal token needs a fresh passkey touch, asked for before the request
 * (`guarded`). Revoking a personal or a task token removes access, so it needs none.
 * @packageDocumentation
 */
import { api } from "./client";
import type { CursorPage } from "./manage";
import { guarded } from "./stepUpFirst";

/** A personal token, as the token lists answer it; never its secret. */
export interface TokenOut {
  /** The token's key id. */
  readonly key_id: string;
  /** The name its owner gave it. */
  readonly name: string;
  /** Its owner's principal name. */
  readonly owner: string;
  /** When it was made. */
  readonly created_at: string;
  /** When it was last used; null when never, or when the server does not know. */
  readonly last_used_at: string | null;
  /** When it stops working; null for never. */
  readonly expires_at: string | null;
  /** When it was revoked; null while it was not. */
  readonly revoked_at: string | null;
}

/** A personal token just made: the only answer that holds the token itself. */
export interface TokenIssueOut {
  /** The token's key id. */
  readonly key_id: string;
  /** Its name. */
  readonly name: string;
  /** The token, shown once and never kept. */
  readonly token: string;
}

/** A live task token, as `GET /api/v1/task-tokens` lists it; never its secret. */
export interface TaskTokenOut {
  /** The token's key id. */
  readonly key_id: string;
  /** The principal it acts as. */
  readonly principal: string;
  /** The environment it is bound to. */
  readonly environment: string;
  /** The asset patterns it may write. */
  readonly asset_scope: readonly string[];
  /** When it was minted. */
  readonly issued_at: string;
  /** When it stops working. */
  readonly expires_at: string;
  /** The run it was minted for; null for one minted by hand. */
  readonly run_id: string | null;
  /** The job of that run; null without one. */
  readonly job: string | null;
  /** The asset it was minted for; null without one. */
  readonly asset: string | null;
}

/** How many tokens a page of either list holds. */
const TOKENS_PAGE = 50;

/**
 * The API path of a principal's tokens, or of one of them.
 *
 * @param owner - The principal's name.
 * @param keyId - The token's key id, for one token.
 * @returns The path.
 */
function tokensApiPath(owner: string, keyId?: string): string {
  const base = `/api/v1/principals/${encodeURIComponent(owner)}/tokens`;
  return keyId === undefined ? base : `${base}/${encodeURIComponent(keyId)}`;
}

/**
 * The query of a page: the filter when given, the page size and the cursor.
 *
 * @param filter - The filter's name and value; a null value leaves it out.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The query, without its `?`.
 */
function pageQuery(filter: readonly [string, string | null], cursor: string | null): string {
  const params = new URLSearchParams();
  const [name, value] = filter;
  if (value !== null) {
    params.set(name, value);
  }
  params.set("limit", String(TOKENS_PAGE));
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  return params.toString();
}

/**
 * Lists a person's own tokens, live and ended.
 *
 * @param name - The person's own principal name.
 * @returns Every token of theirs.
 * @throws {ApiError} for a refusal.
 */
export function listOwnTokens(name: string): Promise<readonly TokenOut[]> {
  return api.request<readonly TokenOut[]>("GET", tokensApiPath(name));
}

/**
 * Makes a personal token for oneself, after the passkey touch.
 *
 * @param name - The person's own principal name.
 * @param label - The token's name.
 * @returns The token, the only time it is shown.
 * @throws {ApiError} for a refusal, such as 409 for a name already taken.
 */
export function createOwnToken(name: string, label: string): Promise<TokenIssueOut> {
  return guarded<TokenIssueOut>("POST", tokensApiPath(name), { name: label });
}

/**
 * Revokes a personal token; it stops working at once.
 *
 * @param owner - The token's owner.
 * @param keyId - The token's key id.
 * @throws {ApiError} for a refusal, such as 404 for a token that is gone.
 */
export async function revokeToken(owner: string, keyId: string): Promise<void> {
  await api.request("DELETE", tokensApiPath(owner, keyId));
}

/**
 * Lists one page of everyone's live personal tokens, newest first.
 *
 * @param owner - Only this principal's; null for everyone's.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal, such as 404 for an unknown owner.
 */
export async function listAllTokens(
  owner: string | null,
  cursor: string | null,
): Promise<CursorPage<TokenOut>> {
  const page = await api.page(`/api/v1/tokens?${pageQuery(["owner", owner], cursor)}`);
  return { items: page.body as readonly TokenOut[], nextCursor: page.nextCursor };
}

/**
 * Lists one page of the live task tokens, newest first.
 *
 * @param environment - Only this environment's; null for every environment's.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal.
 */
export async function listTaskTokens(
  environment: string | null,
  cursor: string | null,
): Promise<CursorPage<TaskTokenOut>> {
  const page = await api.page(
    `/api/v1/task-tokens?${pageQuery(["environment", environment], cursor)}`,
  );
  return { items: page.body as readonly TaskTokenOut[], nextCursor: page.nextCursor };
}

/**
 * Revokes a task token; it stops working at once.
 *
 * @param keyId - The token's key id.
 * @throws {ApiError} for a refusal, such as 404 for a token that is gone.
 */
export async function revokeTaskToken(keyId: string): Promise<void> {
  await api.request("DELETE", `/api/v1/task-tokens/${encodeURIComponent(keyId)}`);
}
