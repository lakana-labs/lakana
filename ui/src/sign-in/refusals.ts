/**
 * The sentence for an identity provider's refusal: one per reason the server names, each with
 * its next step.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import { ApiError } from "../api";
import { problemText } from "./messages";
import "./oidcStrings";

/** The reasons of `OidcSignInRefused` the interface has its own words for. */
const REASONS = new Set([
  "not_linked",
  "deactivated",
  "provider_unreachable",
  "provider_refused",
  "token_invalid",
  "flow_expired",
  "identity_taken",
  "identity_mismatch",
  "link_unusable",
  "last_unlinked_admin",
  "cannot_link_operator",
  "already_linked",
  "auth_too_old",
]);

/**
 * Names why the provider's sign-in, link or confirmation was refused.
 *
 * @param t - The translation function.
 * @param error - What was thrown.
 * @returns The sentence: the interface's for a known reason, else {@link problemText}'s.
 */
export function refusalText(t: TFunction, error: unknown): string {
  if (
    error instanceof ApiError &&
    error.type === "OidcSignInRefused" &&
    error.reason !== undefined &&
    REASONS.has(error.reason)
  ) {
    return t(`oidc.refused.${error.reason}`);
  }
  return problemText(t, error);
}
