/**
 * The layout of every place in Settings: the list of places beside the place, or folded into
 * Section above it on a narrow screen.
 *
 * @packageDocumentation
 */
import { type JSX, useMemo } from "react";
import { Outlet } from "react-router";

import { mySecretEnvironments } from "../api/secrets";
import { useFolded } from "../shell/useFolded";
import { useMe } from "../shell/me";
import { useResource } from "../shell/useResource";
import { PlaceFold } from "./PlaceFold";
import { PlaceList } from "./PlaceList";
import type { SettingsViewer } from "./places";
import "./settings.css";
import "./settingsStrings";
import { SettingsViewerContext } from "./viewer";

/** Nothing: what a failed read of the environments gives, so no place is offered on a guess. */
const NONE: readonly string[] = [];

/**
 * Renders the list (wide) or Section (narrow), then the place. Whether the person may see the
 * place is decided inside the place's own route (`SettingsScreen`), not here.
 *
 * @remarks
 * It reads, once per visit to Settings, where the person manages secrets (`GET
 * /api/v1/whoami`), and provides the viewer the rules read; until the answer comes, the places
 * that need it are neither listed nor refused.
 * @returns The layout.
 */
export function SettingsLayout(): JSX.Element {
  const { admin, noAccessYet } = useMe();
  const secrets = useResource(mySecretEnvironments);
  const secretEnvironments = secrets.data ?? (secrets.error === null ? null : NONE);
  const viewer = useMemo<SettingsViewer>(
    () => ({ admin, noAccessYet, secretEnvironments }),
    [admin, noAccessYet, secretEnvironments],
  );
  const folded = useFolded();
  return (
    <SettingsViewerContext value={viewer}>
      <div className="settings">
        {!folded && (
          <div className="settings-places">
            <PlaceList />
          </div>
        )}
        <div className="settings-place">
          {folded && <PlaceFold />}
          <Outlet />
        </div>
      </div>
    </SettingsViewerContext>
  );
}
