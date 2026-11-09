/**
 * The words for what went wrong while signing in: each names the cause and the next step.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import { ApiError } from "../api";

/** The browser's WebAuthn refusals the interface names, with the key of their sentence. */
const BROWSER_REFUSALS: Readonly<Record<string, string>> = {
  NotAllowedError: "passkey.cancelled",
  NotSupportedError: "passkey.unsupported",
  InvalidStateError: "passkey.duplicate",
  SecurityError: "passkey.wrongAddress",
};

/** The reasons a session ends that the interface has its own words for. */
const ENDED_REASONS = new Set([
  "idle",
  "lifetime",
  "sign_out",
  "sign_out_everywhere",
  "ended_by_admin",
  "replaced",
  "deactivated",
  "token_revoked",
  "recovery",
  "passkey_copied",
  "passkey_removed",
  "reactivated",
]);

/**
 * Names what went wrong: a passkey refusal of the browser, an unreachable server, or the
 * server's own sentence, which names the cause and the next step.
 *
 * @param t - The translation function.
 * @param error - What was thrown.
 * @returns The sentence to show.
 */
export function problemText(t: TFunction, error: unknown): string {
  if (error instanceof DOMException) {
    const key = BROWSER_REFUSALS[error.name];
    return key === undefined ? t("passkey.failed", { name: error.name }) : t(key);
  }
  if (error instanceof ApiError) {
    return error.type === "NetworkError" ? t("errors.unreachable") : error.detail;
  }
  return t("errors.unexpected");
}

/**
 * Says why the browser is signed out.
 *
 * @param t - The translation function.
 * @param error - What reading the session threw.
 * @returns The sentence, or undefined when the browser simply had no session.
 */
export function endedReason(t: TFunction, error: unknown): string | undefined {
  if (error instanceof ApiError && error.status === 401) {
    if (error.reason === undefined) {
      return undefined;
    }
    return ENDED_REASONS.has(error.reason) ? t(`session.ended.${error.reason}`) : error.detail;
  }
  return problemText(t, error);
}
