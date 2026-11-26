/**
 * Whether an access file manages the whole instance, for a place whose writes it would refuse
 * (Groups, a new service account, a service account's deactivation), and the note that says so.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { AccessStatusOut, ManagedScopeOut } from "../api/accessFile";
import { ReadFailed } from "../catalog/ReadFailed";
import type { Resource } from "../shell/useResource";
import "./accessStrings";
import { managedScopeOf } from "./managed";
import { ManagedNote } from "./ManagedNote";

/**
 * The instance's managed scope, as the access files' status says.
 *
 * @param status - The status, as `useAccessStatus` reads it.
 * @returns Undefined until the status is read (or when it could not be), null when no file
 *   manages the instance, else its scope.
 */
export function managedInstance(
  status: Resource<AccessStatusOut>,
): ManagedScopeOut | null | undefined {
  return status.data === undefined ? undefined : managedScopeOf(status.data.scopes, null);
}

/** Properties of {@link InstanceManagedNote}. */
export interface InstanceManagedNoteProps {
  /** The access files' status. */
  readonly status: Resource<AccessStatusOut>;
  /** The commands an administrator runs in an emergency, each with its own Copy. */
  readonly commands: readonly string[];
  /** Reads the status again, after a failed read. */
  readonly onRetry: () => void;
}

/**
 * Renders why nothing can be changed: the status could not be read (with Try again), or an
 * access file manages the instance (the file, and the emergency commands). Nothing otherwise.
 *
 * @param props - The status, the commands and the handler.
 * @param props.status - The access files' status.
 * @param props.commands - The emergency commands.
 * @param props.onRetry - Reads the status again.
 * @returns The note, or nothing.
 */
export function InstanceManagedNote({
  status,
  commands,
  onRetry,
}: InstanceManagedNoteProps): JSX.Element {
  const { t } = useTranslation();
  const managed = managedInstance(status);
  return (
    <>
      {status.error !== null && (
        <ReadFailed
          message={t("access.managed.readFailed", { sentence: status.error })}
          onRetry={onRetry}
        />
      )}
      {managed != null && <ManagedNote scope={managed} commands={commands} />}
    </>
  );
}
