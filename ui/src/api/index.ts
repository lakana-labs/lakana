/**
 * The REST API as the interface uses it; nothing outside `src/api/` calls `fetch`.
 *
 * @remarks
 * Importing this module connects the step-up ceremony to `api.request`, so a request that
 * needs a fresh passkey touch asks for one and is sent once more.
 * @packageDocumentation
 */
import { setStepUp } from "./client";
import { stepUp } from "./passkeys";

setStepUp(stepUp);

export {
  createInvite,
  createRecoveryLink,
  getPrincipal,
  isAdministrator,
  listInvites,
  listPeople,
  listRecoveryLinks,
  PEOPLE_LIMIT,
  withdrawInvite,
  withdrawRecoveryLink,
} from "./access";
export type { InviteOut, RecoveryLinkOut } from "./access";
export { endSessions, listPasskeys, listSessions, removePasskey } from "./account";
export type { PasskeyOut } from "./account";
export { ApiError, onUnauthorized } from "./client";
export { describeLink, redeemLink } from "./links";
export type { LinkDescriptionOut } from "./links";
export { addPasskey, signInWithPasskey } from "./passkeys";
export {
  dismissNotice,
  getSession,
  listNotices,
  myAccess,
  signOut,
  signOutEverywhere,
} from "./session";
export type { AccessOut, NoticeOut, SessionOut } from "./session";
export { changeSignInSettings, getSignInSettings } from "./settings";
export type { SignInSettingsOut } from "./settings";
