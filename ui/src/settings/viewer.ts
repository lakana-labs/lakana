/**
 * What Settings' rules know about the person, read once per visit to Settings.
 *
 * @remarks
 * `SettingsLayout` provides it: the first load's access, and the environments where the person
 * manages secrets and whether they manage row filters and masks, read by Settings' own chunk so the first load does not grow.
 * @packageDocumentation
 */
import { createContext, useContext } from "react";

import type { SettingsViewer } from "./places";

/** Outside Settings: nobody, with no rights and nothing read yet. */
const NOBODY: SettingsViewer = {
  admin: false,
  noAccessYet: false,
  secretEnvironments: null,
  managePolicy: null,
};

/** The viewer of Settings; `SettingsLayout` provides it. */
export const SettingsViewerContext = createContext<SettingsViewer>(NOBODY);

/**
 * Reads what Settings' rules know about the person.
 *
 * @returns The viewer.
 */
export function useSettingsViewer(): SettingsViewer {
  return useContext(SettingsViewerContext);
}
