/**
 * The address's search parameters, and a change to them that builds on the address as it is
 * now, not as the last render saw it.
 *
 * @packageDocumentation
 */
import { useCallback } from "react";
import { type Location, type NavigateOptions, useLocation, useSearchParams } from "react-router";

/** A change of the parameters: it gets a copy of the current ones and returns the next. */
export type ParamsChange = (current: URLSearchParams) => URLSearchParams;

/** Applies a change to the address's parameters, with the router's options of the navigation. */
export type UpdateParams = (change: ParamsChange, options?: NavigateOptions) => void;

/**
 * A change that sets or removes a parameter of the address.
 *
 * @param name - The parameter.
 * @param value - Its value; null or empty removes it.
 * @returns The change.
 */
export function settingParam(name: string, value: string | null): ParamsChange {
  return (current) => {
    if (value === null || value === "") {
      current.delete(name);
    } else {
      current.set(name, value);
    }
    return current;
  };
}

/**
 * The last change written, and the rendered location it was written over. Until a render shows
 * the address it led to, a further change builds on it; shared by every user of the hook, so
 * two filters of one screen changed at once both reach the address.
 */
let written: { readonly over: Location; readonly search: string } | null = null;

/**
 * The address's parameters and a change to them. React Router's own updater gets the
 * parameters of the last render, so a second change made before the first has rendered (a
 * Period chosen and a job's name typed at once) would overwrite the first (#744). This one
 * builds on the change still to render, when there is one.
 *
 * @returns The parameters and the change.
 */
export function useAddressParams(): readonly [URLSearchParams, UpdateParams] {
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const update = useCallback<UpdateParams>(
    (change, options) => {
      const base = written?.over === location ? written.search : location.search;
      const next = change(new URLSearchParams(base));
      written = { over: location, search: next.toString() };
      setParams(next, options);
    },
    [location, setParams],
  );
  return [params, update];
}
