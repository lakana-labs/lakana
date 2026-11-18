/**
 * Settings' list of places: the places this person is shown, a group's and a page's children
 * indented beneath them.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import {
  listedPlaces,
  placeAt,
  placePath,
  SETTINGS_PLACES,
  type SettingsPlace,
  type SettingsPlaceId,
} from "./places";
import "./settingsStrings";
import { useSettingsViewer } from "./viewer";

/** Properties of {@link PlaceList}. */
export interface PlaceListProps {
  /** The registry; only a test passes another. */
  readonly places?: readonly SettingsPlace[];
}

/** Properties of {@link Places}. */
interface PlacesProps {
  /** The places shown, in list order. */
  readonly listed: readonly SettingsPlace[];
  /** The place whose children these are, or null for the top of the list. */
  readonly parent: SettingsPlaceId | null;
  /** The id of the place the address is in, or null. */
  readonly current: SettingsPlaceId | null;
}

/**
 * Renders one level of the list, and each place's children nested below it. A group is a plain
 * label, not a link, so Tab passes over it.
 *
 * @param props - The places, the level and the current place.
 * @param props.listed - The places shown.
 * @param props.parent - The place whose children these are.
 * @param props.current - The current place's id.
 * @returns The list, or null when the level is empty.
 */
function Places({ listed, parent, current }: PlacesProps): JSX.Element | null {
  const { t } = useTranslation();
  const level = listed.filter((place) => place.parent === parent);
  if (level.length === 0) return null;
  return (
    <ul className="place-items">
      {level.map((place) => (
        <li key={place.id}>
          {place.kind === "group" ? (
            <span className="place-group">{t(place.label)}</span>
          ) : (
            <Link
              className="place-link"
              href={placePath(place)}
              aria-current={place.id === current ? "page" : false}
            >
              {t(place.label)}
            </Link>
          )}
          <Places listed={listed} parent={place.id} current={current} />
        </li>
      ))}
    </ul>
  );
}

/**
 * Renders the navigation "Settings places": the listed places in registry order, the one the
 * address is in marked with `aria-current="page"`, an accent bar and a heavier weight. The
 * person, from {@link useSettingsViewer}, is shown only the places they may see.
 *
 * @param props - The registry.
 * @param props.places - The registry.
 * @returns The navigation.
 */
export function PlaceList({ places = SETTINGS_PLACES }: PlaceListProps): JSX.Element {
  const { t } = useTranslation();
  const viewer = useSettingsViewer();
  const { pathname } = useLocation();
  const current = placeAt(pathname, places)?.id ?? null;
  return (
    <nav className="place-list" aria-label={t("settings.places.label")}>
      <Places listed={listedPlaces(viewer, places)} parent={null} current={current} />
    </nav>
  );
}
