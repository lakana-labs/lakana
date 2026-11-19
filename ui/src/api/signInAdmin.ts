/**
 * The writes of Settings › Sign-in that the server guards with a passkey touch: the session
 * lengths, a new invite and a new recovery link.
 *
 * @remarks
 * Its own module, loaded only with Sign-in's places, so the first load does not carry it or
 * `guarded`. Each asks for the touch before the request (`guarded`); a cancelled touch sends
 * nothing. A new link is in the answer that issues it and nowhere else: the interface shows it
 * once and never keeps it. Withdrawing an invite or a link removes access, so it needs no touch
 * (`withdrawInvite` and `withdrawRecoveryLink` in `access.ts`).
 * @packageDocumentation
 */
import type { InviteOut, RecoveryLinkOut } from "./access";
import type { SignInSettingsIn, SignInSettingsOut } from "./settings";
import { guarded } from "./stepUpFirst";

/**
 * Changes the session lengths, after the passkey touch; sessions that begin afterwards use them.
 *
 * @param lengths - The new lengths.
 * @returns The settings as saved.
 * @throws {ApiError} for a refusal; whatever the ceremony throws.
 */
export async function saveSignInSettings(lengths: SignInSettingsIn): Promise<SignInSettingsOut> {
  return guarded<SignInSettingsOut>("PATCH", "/api/v1/settings/sign-in", lengths);
}

/**
 * Invites a new person, after the passkey touch.
 *
 * @param name - The principal name the invite creates.
 * @param displayName - The name to show; blank for none.
 * @returns The invite with its link.
 * @throws {ApiError} for a refusal; whatever the ceremony throws.
 */
export async function issueInvite(name: string, displayName: string): Promise<InviteOut> {
  const shown = displayName.trim();
  return guarded<InviteOut>("POST", "/api/v1/invites", {
    name,
    display_name: shown === "" ? null : shown,
  });
}

/**
 * Issues a recovery link for a person who is not an administrator, after the passkey touch.
 *
 * @param name - The person's principal name.
 * @returns The link.
 * @throws {ApiError} for a refusal; whatever the ceremony throws.
 */
export async function issueRecoveryLink(name: string): Promise<RecoveryLinkOut> {
  return guarded<RecoveryLinkOut>(
    "POST",
    `/api/v1/principals/${encodeURIComponent(name)}/recovery-link`,
  );
}
