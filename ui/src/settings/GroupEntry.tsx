/**
 * The address of a group of places in Settings (`/settings/access`), which has no page of its
 * own: it opens the group's first listed place.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Navigate } from "react-router";

import { NotAvailable } from "../shell/NotAvailable";
import { useMe } from "../shell/me";
import { REDIRECT } from "../shell/redirect";
import { entryPath, type SettingsPlace } from "./places";
import "./settingsStrings";

/** Properties of {@link GroupEntry}. */
export interface GroupEntryProps {
  /** The group. */
  readonly group: SettingsPlace;
  /** The registry; only a test passes another. */
  readonly places?: readonly SettingsPlace[];
}

/**
 * Goes on to the group's first listed place, replacing the group's address so Back skips it;
 * when the person is shown none of its places, says the group is not theirs to use.
 *
 * @param props - The group and the registry.
 * @param props.group - The group.
 * @param props.places - The registry.
 * @returns The redirect, or the permission state.
 */
export function GroupEntry({ group, places }: GroupEntryProps): JSX.Element {
  const { t } = useTranslation();
  const { admin, noAccessYet } = useMe();
  const to = entryPath(group, { admin, noAccessYet }, places);
  return to === null ? (
    <NotAvailable place={t(group.label)} />
  ) : (
    <Navigate to={to} replace state={REDIRECT} />
  );
}
