/**
 * What the signed-in person may do, for the screens that change with it (Home).
 *
 * @packageDocumentation
 */
import { createContext, useContext } from "react";

import type { AccessOut } from "../api";

/** What a screen assumes outside the shell: nobody, with no rights. */
const NOBODY: AccessOut = { name: "", admin: false, noAccessYet: false, mayDeploy: false };

/** The signed-in person's access; the shell provides it. */
export const MeContext = createContext<AccessOut>(NOBODY);

/**
 * Reads what the signed-in person may do.
 *
 * @returns The person's name and access.
 */
export function useMe(): AccessOut {
  return useContext(MeContext);
}
