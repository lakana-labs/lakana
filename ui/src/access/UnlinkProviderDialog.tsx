/**
 * The question before an administrator unlinks a person from the identity provider: what goes,
 * what comes next, and that offboarding is a deactivation.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { unlinkProvider } from "../api/providerSession";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import "./accessStrings";

/** Properties of {@link UnlinkProviderDialog}. */
export interface UnlinkProviderDialogProps {
  /** The person's principal name. */
  readonly name: string;
  /** The provider's name. */
  readonly provider: string;
  /** Closes the dialog without unlinking. */
  readonly onCancel: () => void;
  /** Called once the person is unlinked. */
  readonly onDone: () => void;
}

/**
 * Renders the dialog. Unlink asks for the passkey touch first when the session's is stale.
 *
 * @param props - The person, the provider and the handlers.
 * @param props.name - The person's principal name.
 * @param props.provider - The provider's name.
 * @param props.onCancel - Closes without unlinking.
 * @param props.onDone - Called once the person is unlinked.
 * @returns The dialog.
 */
export function UnlinkProviderDialog({
  name,
  provider,
  onCancel,
  onDone,
}: UnlinkProviderDialogProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <ConfirmDialog
      title={t("access.provider.unlinkTitle", { name, provider })}
      confirmLabel={t("access.provider.unlinkConfirm")}
      onConfirm={() => unlinkProvider(name)}
      onCancel={onCancel}
      onDone={onDone}
    >
      <p>{t("access.provider.unlinkGoes", { name })}</p>
      <p>{t("access.provider.unlinkThen", { name })}</p>
      <p>{t("access.provider.offboard")}</p>
    </ConfirmDialog>
  );
}
