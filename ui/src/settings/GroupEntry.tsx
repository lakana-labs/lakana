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
import { REDIRECT } from "../shell/redirect";
import { entryPath, placeState, SETTINGS_PLACES, type SettingsPlace } from "./places";
import "./settingsStrings";
import { useSettingsViewer } from "./viewer";

/** Properties of {@link GroupEntry}. */
export interface GroupEntryProps {
  /** The group. */
  readonly group: SettingsPlace;
  /** The registry; only a test passes another. */
  readonly places?: readonly SettingsPlace[];
}

/**
 * Goes on to the group's first listed place, replacing the group's address so Back skips it;
 * when the person is shown none of its places, says the group is not theirs to use. While a
 * place before the first listed one waits for its rule, it shows nothing: a person who manages
 * secrets lands on Secrets, not on the place after it.
 *
 * @param props - The group and the registry.
 * @param props.group - The group.
 * @param props.places - The registry.
 * @returns The redirect, the permission state, or nothing while pending.
 */
export function GroupEntry({
  group,
  places = SETTINGS_PLACES,
}: GroupEntryProps): JSX.Element | null {
  const { t } = useTranslation();
  const viewer = useSettingsViewer();
  const children = places.filter((place) => place.parent === group.id);
  const first = children.find((place) => placeState(place, viewer, places) !== "hidden");
  if (first !== undefined && placeState(first, viewer, places) === "pending") return null;
  const to = entryPath(group, viewer, places);
  return to === null ? (
    <NotAvailable place={t(group.label)} />
  ) : (
    <Navigate to={to} replace state={REDIRECT} />
  );
}
