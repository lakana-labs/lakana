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
export { getTableTree, listTables, TABLE_SORTS } from "./catalog";
export type {
  HealthSummaryOut,
  OwnerOut,
  SortOrder,
  TableListItemOut,
  TableMatchOut,
  TableSort,
  TableTreeOut,
} from "./catalog";
export { ApiError, onUnauthorized } from "./client";
export { getAttention, getGetStarted, getWork } from "./home";
export type { AttentionKind, GetStartedOut, GetStartedStepId, WorkRepositoryOut } from "./home";
export { describeLink, redeemLink } from "./links";
export type { LinkDescriptionOut } from "./links";
export { changeInterfaceState, getInfo, getInterfaceState, listRecent, recordOpened } from "./me";
export type { InterfaceStateOut, RecentOut } from "./me";
export { addPasskey, signInWithPasskey } from "./passkeys";
export {
  PICTURE_MAX_BYTES,
  PICTURE_TYPES,
  changedPicture,
  pictureUrl,
  releasePicture,
  removePicture,
  setPicture,
  watchPictures,
} from "./pictures";
export {
  dismissNotice,
  getSession,
  listNotices,
  myAccess,
  signOut,
  signOutEverywhere,
} from "./session";
export type { AccessOut, NoticeOut, SessionOut } from "./session";
export {
  createRepository,
  generateDeployKey,
  getRepository,
  requestProbe,
  waitForProbe,
} from "./repositories";
export type {
  DeployKeyOut,
  HostKeyOut,
  RepositoryCreate,
  RepositoryProbeIn,
  RepositoryProbeOut,
} from "./repositories";
export { changeSignInSettings, getSignInSettings } from "./settings";
export type { SignInSettingsOut } from "./settings";
export { getLineage, getTable, getTableHealth, myPermissions } from "./table";
export type {
  ColumnOut,
  GraphNodeOut,
  GraphOut,
  ModelReferenceOut,
  PermissionsOut,
  TableHealthOut,
  TableOut,
  TableRef,
} from "./table";
export { getStatus } from "./status";
export type { InstanceStatusOut, StatusDetailOut } from "./status";
