/**
 * The note on a place whose scope an access file manages: which file, since when, how to change
 * it, and the command for an emergency.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Trans, useTranslation } from "react-i18next";

import type { ManagedScopeBriefOut } from "../api/accessFile";
import { CopyLine } from "../shell/CopyLine";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./accessStrings";
import { GitMark } from "./GitMark";
import { INSTANCE_SCOPE } from "./managed";

/** Properties of {@link ManagedNote}. */
export interface ManagedNoteProps {
  /** The managed scope. */
  readonly scope: ManagedScopeBriefOut;
  /** The commands an administrator runs in an emergency, each with its own Copy. */
  readonly commands: readonly string[];
}

/**
 * Renders the mark, the sentence naming the file and when it was applied, and the emergency
 * commands to copy. The interface itself never overrides a managed scope.
 *
 * @param props - The scope and the commands.
 * @param props.scope - The managed scope.
 * @param props.commands - The emergency commands.
 * @returns The note.
 */
export function ManagedNote({ scope, commands }: ManagedNoteProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  return (
    <div className="managed-note">
      <p>
        <GitMark />{" "}
        <Trans
          t={t}
          i18nKey={
            scope.scope === INSTANCE_SCOPE
              ? "access.managed.instance"
              : "access.managed.environment"
          }
          values={{ when: when(scope.applied_at) }}
          components={{
            code: <code />,
            file: <VisibleText text={scope.file_name} />,
            environment: <VisibleText text={scope.scope} />,
          }}
        />
      </p>
      {commands.map((command) => (
        <CopyLine key={command} text={command} copyLabel={t("access.managed.copy")} />
      ))}
    </div>
  );
}
