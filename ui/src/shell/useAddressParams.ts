/**
 * The address's search parameters, and a change to them that builds on the address as it is
 * now, not as the last render saw it.
 *
 * @packageDocumentation
 */
import { useCallback, useContext } from "react";
import {
  type Location,
  type NavigateOptions,
  UNSAFE_DataRouterContext,
  UNSAFE_NavigationContext,
  useNavigate,
  useSearchParams,
} from "react-router";

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
 * The change written last, while the router still works on it. A data router (the interface's)
 * moves to a new address a moment after the call, so a second change in that moment builds on
 * this one; shared by every user of the hook, so two filters of one screen changed at once both
 * reach the address. It is forgotten when its navigation ends, landed or overtaken (a Back made
 * before it landed): from then on the router's own address is the base.
 */
let pending: { readonly search: string; readonly token: object } | null = null;

/**
 * The address's parameters and a change to them. React Router's own updater gets the
 * parameters of the last render, so a second change made before the first has rendered (a
 * Period chosen and a job's name typed at once) would overwrite the first (#744). This one
 * builds on the router's address as it is now: the change it still works on, else its current
 * location (a data router's state, or a declarative router's history), which a Back or any other
 * navigation has already moved, whether or not it has rendered.
 *
 * @returns The parameters and the change.
 */
export function useAddressParams(): readonly [URLSearchParams, UpdateParams] {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const data = useContext(UNSAFE_DataRouterContext);
  const { navigator } = useContext(UNSAFE_NavigationContext);
  const update = useCallback<UpdateParams>(
    (change, options) => {
      // A data router's state has the address once its navigation landed; the memory or browser
      // history under any other router (each router of the interface is one or the other) has it
      // at once.
      const now =
        data === null
          ? (navigator as typeof navigator & { readonly location: Location }).location
          : data.router.state.location;
      const next = change(new URLSearchParams(pending?.search ?? now.search)).toString();
      const done = navigate(`?${next}`, options);
      if (done instanceof Promise) {
        const token = {};
        pending = { search: next, token };
        void done.finally(() => {
          if (pending?.token === token) {
            pending = null;
          }
        });
      }
    },
    [data, navigator, navigate],
  );
  return [params, update];
}
