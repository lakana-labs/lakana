/**
 * The note on a place whose scope an access file manages: which file, since when, how to change
 * it, and the command for an emergency.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { ManagedScopeOut } from "../api/accessFile";
import { CopyLine } from "../shell/CopyLine";
import { useWhen } from "../shell/useWhen";
import "./accessStrings";
import { GitMark } from "./GitMark";
import { INSTANCE_SCOPE } from "./managed";

/** Properties of {@link ManagedNote}. */
export interface ManagedNoteProps {
  /** The managed scope. */
  readonly scope: ManagedScopeOut;
  /** The command an administrator runs in an emergency. */
  readonly command: string;
}

/**
 * Renders the mark, the sentence naming the file and when it was applied, and the emergency
 * command to copy. The interface itself never overrides a managed scope.
 *
 * @param props - The scope and the command.
 * @param props.scope - The managed scope.
 * @param props.command - The emergency command.
 * @returns The note.
 */
export function ManagedNote({ scope, command }: ManagedNoteProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const values = { file: scope.file_name, when: when(scope.applied_at) };
  return (
    <div className="managed-note">
      <p>
        <GitMark />{" "}
        {scope.scope === INSTANCE_SCOPE
          ? t("access.managed.instance", values)
          : t("access.managed.environment", { ...values, environment: scope.scope })}
      </p>
      <CopyLine text={command} copyLabel={t("access.managed.copy")} />
    </div>
  );
}
