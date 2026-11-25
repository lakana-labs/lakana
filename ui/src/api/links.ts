/**
 * Sign-in links: describe one by its public id, and use it with its secret.
 *
 * @remarks
 * A link is `<address>/sign-in#l=<public id>&s=<secret>`. Describing it never uses it, so a link
 * preview or a mail scanner opening the page cannot; only {@link redeemLink}, sent when the
 * person confirms, does, with the secret in the body and never in a URL.
 * @packageDocumentation
 */
import { api, setCsrfToken } from "./client";

/** A link as `POST /api/v1/sign-in/links/describe` answers it. */
export interface LinkDescriptionOut {
  /** What the link is for. The server may add values: compare, and keep a fallback. */
  readonly kind: "setup" | "invite" | "recovery" | "local";
  /** The principal name the link signs in as (an invite: the account it creates). */
  readonly name: string;
  /** The name to show, when there is one. */
  readonly display_name: string | null;
  /**
   * `open` for a usable link (the only one the server describes). The server may add values: compare, and keep a fallback.
   */
  readonly state: "open" | "used" | "expired" | "withdrawn";
  /**
   * True when the link works only through the identity provider: a recovery link of a person
   * linked to it. Absent from a server older than #32.
   */
  readonly provider_only?: boolean;
  /** True when the page offers "Sign in with <provider>" for the link; absent before #32. */
  readonly offers_provider?: boolean;
}

/** What signing in answers. */
export interface SignInOut {
  /** The principal name signed in as. */
  readonly name: string;
  /** True when the person must add a passkey before anything else. */
  readonly needs_passkey: boolean;
  /** True when the interface may offer to add a passkey. */
  readonly offer_passkey: boolean;
  /** The new session's CSRF token. */
  readonly csrf_token: string;
}

/**
 * Describes a link without using it.
 *
 * @param linkId - The link's public id (`l=`).
 * @returns The link.
 * @throws {ApiError} 410 with `state` when the link cannot be used.
 */
export async function describeLink(linkId: string): Promise<LinkDescriptionOut> {
  return api.request<LinkDescriptionOut>("POST", "/api/v1/sign-in/links/describe", {
    link_id: linkId,
  });
}

/**
 * Uses a link: signs in, and keeps the new session's CSRF token.
 *
 * @param linkId - The link's public id (`l=`).
 * @param secret - The link's secret (`s=`).
 * @returns Who is signed in and whether a passkey must or may be added now.
 * @throws {ApiError} 410 with `state` when the link cannot be used.
 */
export async function redeemLink(linkId: string, secret: string): Promise<SignInOut> {
  const signedIn = await api.request<SignInOut>("POST", "/api/v1/sign-in/links/redeem", {
    link_id: linkId,
    secret,
  });
  setCsrfToken(signedIn.csrf_token);
  return signedIn;
}
