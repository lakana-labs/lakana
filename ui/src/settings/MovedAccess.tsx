/**
 * The stand-in for every old `/access/*` address: it leads to the place in Settings.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Navigate, useLocation } from "react-router";

import { movedAccessPath } from "./moved";

/**
 * Redirects to the new address, keeping the search and the hash and replacing the old entry, so
 * Back never returns to it.
 *
 * @returns The redirect.
 */
export function MovedAccess(): JSX.Element {
  const { pathname, search, hash } = useLocation();
  return <Navigate replace to={{ pathname: movedAccessPath(pathname), search, hash }} />;
}
