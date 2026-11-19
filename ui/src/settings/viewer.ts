/**
 * What Settings' rules know about the person, read once per visit to Settings.
 *
 * @remarks
 * `SettingsLayout` provides it: the first load's access, and the environments where the person
 * manages secrets, whether they manage row filters and masks, whether they read an environment
 * and what they may do on the instance, read by Settings' own chunk so the first load does not
 * grow.
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
  readsEnvironments: null,
  instanceActions: null,
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
