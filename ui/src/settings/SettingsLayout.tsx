/**
 * The layout of every place in Settings: the list of places beside the place, or folded into
 * Section above it on a narrow screen.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Outlet, useLocation } from "react-router";

import { NotAvailable } from "../shell/NotAvailable";
import { useFolded } from "../shell/useFolded";
import { useMe } from "../shell/me";
import { PlaceFold } from "./PlaceFold";
import { PlaceList } from "./PlaceList";
import { listedPlaces, placeAt, type SettingsViewer } from "./places";
import "./settings.css";
import "./settingsStrings";

/**
 * Renders the list (wide) or Section (narrow), then the place. A place the person may not see
 * renders {@link NotAvailable} with its name instead, so its page is never loaded and its route
 * never read.
 *
 * @returns The layout.
 */
export function SettingsLayout(): JSX.Element {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const { admin, noAccessYet } = useMe();
  const viewer: SettingsViewer = { admin, noAccessYet };
  const folded = useFolded();
  const place = placeAt(pathname);
  const hidden = place !== null && !listedPlaces(viewer).includes(place);
  return (
    <div className="settings">
      {!folded && (
        <aside className="settings-places">
          <PlaceList viewer={viewer} />
        </aside>
      )}
      <div className="settings-place">
        {folded && <PlaceFold viewer={viewer} />}
        {hidden ? <NotAvailable place={t(place.label)} /> : <Outlet />}
      </div>
    </div>
  );
}
