/**
 * The layout of every place in Settings: the list of places beside the place, or folded into
 * Section above it on a narrow screen.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Outlet } from "react-router";

import { useFolded } from "../shell/useFolded";
import { useMe } from "../shell/me";
import { PlaceFold } from "./PlaceFold";
import { PlaceList } from "./PlaceList";
import type { SettingsViewer } from "./places";
import "./settings.css";
import "./settingsStrings";

/**
 * Renders the list (wide) or Section (narrow), then the place. Whether the person may see the
 * place is decided inside the place's own route (`SettingsScreen`), not here.
 *
 * @returns The layout.
 */
export function SettingsLayout(): JSX.Element {
  const { admin, noAccessYet } = useMe();
  const viewer: SettingsViewer = { admin, noAccessYet };
  const folded = useFolded();
  return (
    <div className="settings">
      {!folded && (
        <div className="settings-places">
          <PlaceList viewer={viewer} />
        </div>
      )}
      <div className="settings-place">
        {folded && <PlaceFold viewer={viewer} />}
        <Outlet />
      </div>
    </div>
  );
}
