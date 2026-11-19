/**
 * What the Jobs list shows: the environment it is filtered by and the words it is searched for,
 * both in the address.
 *
 * @packageDocumentation
 */
import { useContext, useEffect, useState } from "react";
import {
  NavigationType,
  // A React Router internal, on purpose: its navigator is the one value that stays the same for
  // the router's whole life, and no supported API names the router itself. Used only as an
  // identity (docs/agent/conventions.md, "The `ui/` project").
  UNSAFE_NavigationContext,
  useLocation,
  useNavigationType,
} from "react-router";

import type { EnvironmentChoice } from "../api/environments";
import { settingParam, useAddressParams } from "../shell/useAddressParams";
import { readJobsFilter, writeJobsFilter } from "./remember";

/** The list's filter and search, and how to change them. */
export interface JobsFilter {
  /** The environment the list shows; null for every environment the person may read. */
  readonly environment: string | null;
  /** The words the list is searched for; empty for no search. */
  readonly query: string;
  /** Chooses an environment, or all of them with null: the address and this browser keep it. */
  readonly setEnvironment: (environment: string | null) => void;
  /** Searches the list; the address keeps the words. */
  readonly setQuery: (query: string) => void;
}

/**
 * How a change reaches the history: `replace` takes the place of the current address, so Back
 * leaves the page with its filter; `push` adds an entry, so Back undoes the change.
 */
export type FilterHistory = "replace" | "push";

/**
 * The routers whose first address has been shown: Back to it later is not a fresh load.
 */
const shown = new WeakSet<object>();

/**
 * Whether the browser reloaded this page. A reload shows the address as it was, as Back does:
 * React Router keeps no key in the first entry of the history, so its reload looks like a new
 * load otherwise. A browser without navigation timing says no.
 *
 * @returns True after a reload.
 */
function reloaded(): boolean {
  const timing: Partial<Pick<Performance, "getEntriesByType">> = performance;
  const [entry] = timing.getEntriesByType?.("navigation") ?? [];
  return (entry as Partial<PerformanceNavigationTiming> | undefined)?.type === "reload";
}

/**
 * The list's environment and search. The environment is the address's `?environment=`, else the
 * one this browser remembers, else all of them; only a listed environment counts, so one the
 * person may no longer read (or one that is gone) names nothing and the list shows all. While
 * the environments are read, none is chosen. A choice writes the address and this browser's
 * storage (all environments as a choice of its own); the search is `?q=` in the address. A list
 * replaces the address with each change. The Runs tab pushes, as a job's page does, so Back
 * steps through its filters. Opened by a link, a redirect or a fresh load at an address without
 * an environment, it writes the remembered one into that address in its place (no step of the
 * history); otherwise (Back, Forward, a reload), and from then on, it reads the environment from the address alone, so
 * Back to an address without one, from the tab or from another page, is all environments.
 *
 * @param environments - The environments the person may read; undefined while they are read.
 * @param history - How a change reaches the history; `replace` by default.
 * @returns The filter, the search and their setters.
 */
export function useJobsFilter(
  environments: readonly EnvironmentChoice[] | undefined,
  history: FilterHistory = "replace",
): JobsFilter {
  const [params, update] = useAddressParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const { navigator } = useContext(UNSAFE_NavigationContext);
  const [remembered, setRemembered] = useState(readJobsFilter);
  // The address the Runs tab opened at, what was remembered then, and whether it is new: reached
  // by a link or a redirect, or the page's first address on a fresh load. Back or Forward to an
  // address shown before (from a run's page, say), or a reload, shows it as it was.
  const [opening] = useState(() => ({
    key: location.key,
    environment: remembered,
    fresh:
      navigationType !== NavigationType.Pop ||
      (location.key === "default" && !shown.has(navigator) && !reloaded()),
  }));
  useEffect(() => {
    shown.add(navigator);
  }, [navigator]);
  // Once the tab has left that address (a change pushed while the environments were read),
  // coming back to it fills in nothing either.
  const [left, setLeft] = useState(false);
  if (!left && location.key !== opening.key) {
    setLeft(true);
  }
  const listed = (name: string | null): name is string =>
    name !== null && environments?.some((environment) => environment.name === name) === true;
  const fromAddress = params.get("environment");
  // The Runs tab opened without an environment, on one remembered and listed: the address takes it.
  const filling =
    history === "push" &&
    opening.fresh &&
    !left &&
    fromAddress === null &&
    location.key === opening.key &&
    listed(opening.environment);
  const fallback = history === "push" ? (filling ? opening.environment : null) : remembered;
  const environment = listed(fromAddress ?? fallback) ? (fromAddress ?? fallback) : null;
  useEffect(() => {
    if (filling) {
      update(settingParam("environment", opening.environment), { replace: true });
    }
  }, [filling, opening.environment, update]);
  return {
    environment,
    query: params.get("q") ?? "",
    setEnvironment: (chosen) => {
      writeJobsFilter(chosen);
      setRemembered(chosen);
      update(settingParam("environment", chosen), { replace: history === "replace" });
    },
    setQuery: (query) => {
      update(settingParam("q", query), { replace: history === "replace" });
    },
  };
}

/**
 * Whether a row matches the search: any of its names holds the words, in any case. An empty
 * search matches every row.
 *
 * @param query - The words searched for.
 * @param names - The row's names, such as its job and its repository; null for none.
 * @returns True when the row is shown.
 */
export function matchesSearch(query: string, ...names: readonly (string | null)[]): boolean {
  const words = query.trim().toLocaleLowerCase();
  return words === "" || names.some((name) => name?.toLocaleLowerCase().includes(words) === true);
}
