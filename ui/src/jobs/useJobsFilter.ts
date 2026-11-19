/**
 * What the Jobs list shows: the environment it is filtered by and the words it is searched for,
 * both in the address.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";
import { useLocation } from "react-router";

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
 * The list's environment and search. The environment is the address's `?environment=`, else the
 * one this browser remembers, else all of them; only a listed environment counts, so one the
 * person may no longer read (or one that is gone) names nothing and the list shows all. While
 * the environments are read, none is chosen. A choice writes the address and this browser's
 * storage (all environments as a choice of its own); the search is `?q=` in the address. A list
 * replaces the address with each change. The Runs tab pushes, as a job's page does, so Back
 * steps through its filters: it opens on the remembered environment, written into its address
 * in place of the address it opened at (no step of the history), and from then on reads the
 * environment from the address alone, so Back to an address without one is all environments.
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
  const [remembered, setRemembered] = useState(readJobsFilter);
  // The address the Runs tab opened at, and what was remembered then.
  const [opening] = useState(() => ({ key: location.key, environment: remembered }));
  const listed = (name: string | null): name is string =>
    name !== null && environments?.some((environment) => environment.name === name) === true;
  const fromAddress = params.get("environment");
  // The Runs tab opened without an environment, on one remembered and listed: the address takes it.
  const filling =
    history === "push" &&
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
