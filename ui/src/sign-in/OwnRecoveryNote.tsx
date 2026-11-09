/**
 * The way back in for an administrator who loses every passkey: the server operator's command.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback } from "react";
import { useTranslation } from "react-i18next";

import { isAdministrator } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { useResource } from "../shell/useResource";

/** Properties of {@link OwnRecoveryNote}. */
export interface OwnRecoveryNoteProps {
  /** The signed-in person's principal name. */
  readonly name: string;
}

/**
 * Tells an administrator, on their own passkeys, that only the server operator can let them back
 * in once every passkey is lost, with the command to run; nobody may recover themselves, and no
 * administrator another one. Shows nothing for anyone else, or when the role cannot be read.
 *
 * @param props - Whose page.
 * @param props.name - The signed-in person's principal name.
 * @returns The note, or nothing.
 */
export function OwnRecoveryNote({ name }: OwnRecoveryNoteProps): JSX.Element | null {
  const { t } = useTranslation();
  const load = useCallback(() => isAdministrator(name).catch(() => false), [name]);
  if (useResource(load).data !== true) {
    return null;
  }
  return (
    <section className="page-notice">
      <p>{t("access.recovery.operatorForYou")}</p>
      <CopyLine text={t("access.recovery.command", { name })} copyLabel={t("comingSoon.copy")} />
    </section>
  );
}
