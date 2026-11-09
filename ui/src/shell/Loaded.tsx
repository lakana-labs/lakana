/**
 * What a screen read: its content once it arrived, and the cause when reading failed.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Resource } from "./useResource";

/** Properties of {@link Loaded}. */
export interface LoadedProps<T> {
  /** What is read. */
  readonly resource: Resource<T>;
  /** Renders what arrived. */
  readonly children: (data: T) => ReactNode;
}

/**
 * Renders "Loading…" until the first answer, then the content; a failure is an alert naming
 * the cause and the next step, above the last content if there was one.
 *
 * @template T - What is read.
 * @param props - The resource and the renderer.
 * @param props.resource - What is read.
 * @param props.children - Renders what arrived.
 * @returns The state.
 */
export function Loaded<T>({ resource, children }: LoadedProps<T>): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {resource.error !== null && (
        <p className="step-error" role="alert">
          {resource.error}
        </p>
      )}
      {resource.data === undefined
        ? resource.error === null && <p className="step-status">{t("session.loading")}</p>
        : children(resource.data)}
    </>
  );
}
