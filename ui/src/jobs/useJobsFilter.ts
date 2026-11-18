/**
 * What the Jobs list shows: the environment it is filtered by and the words it is searched for,
 * both in the address.
 *
 * @packageDocumentation
 */
import { useState } from "react";
import { useSearchParams } from "react-router";

import type { EnvironmentChoice } from "../api/environments";
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
 * Sets or removes a parameter of the address, replacing the address in the history.
 *
 * @param set - The router's setter of the address's parameters.
 * @param name - The parameter.
 * @param value - Its value; null or empty removes it.
 */
function replaceParam(
  set: ReturnType<typeof useSearchParams>[1],
  name: string,
  value: string | null,
): void {
  set(
    (current) => {
      const next = new URLSearchParams(current);
      if (value === null || value === "") {
        next.delete(name);
      } else {
        next.set(name, value);
      }
      return next;
    },
    { replace: true },
  );
}

/**
 * The list's environment and search. The environment is the address's `?environment=`, else the
 * one this browser remembers, else all of them; only a listed environment counts, so one the
 * person may no longer read (or one that is gone) names nothing and the list shows all. While
 * the environments are read, none is chosen. A choice writes the address and this browser's
 * storage; the search is `?q=` in the address.
 *
 * @param environments - The environments the person may read; undefined while they are read.
 * @returns The filter, the search and their setters.
 */
export function useJobsFilter(environments: readonly EnvironmentChoice[] | undefined): JobsFilter {
  const [params, setParams] = useSearchParams();
  const [remembered, setRemembered] = useState(readJobsFilter);
  const listed = (name: string | null): name is string =>
    name !== null && environments?.some((environment) => environment.name === name) === true;
  const fromAddress = params.get("environment");
  const environment =
    fromAddress === null
      ? listed(remembered)
        ? remembered
        : null
      : listed(fromAddress)
        ? fromAddress
        : null;
  return {
    environment,
    query: params.get("q") ?? "",
    setEnvironment: (chosen) => {
      writeJobsFilter(chosen);
      setRemembered(chosen);
      replaceParam(setParams, "environment", chosen);
    },
    setQuery: (query) => {
      replaceParam(setParams, "q", query);
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
