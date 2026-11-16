/**
 * The question before a namespace is deleted, as `lakana ns delete` asks it.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { deleteNamespace } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { ConfirmRow, useConfirm } from "../actions/ConfirmRow";
import { ProtectedReason } from "../actions/ProtectedReason";
import { useProtected } from "../actions/useProtected";
import "./placeStrings";

/** Properties of {@link DeleteNamespaceDialog}. */
export interface DeleteNamespaceDialogProps {
  /** The environment of the namespace. */
  readonly environment: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** Called once it is deleted; the caller decides where to go. */
  readonly onDone: () => void;
  /** Closes the dialog without deleting anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the question. In a protected environment it asks for the reason to break the
 * protection first. From the browser the server asks for a passkey touch, which the request
 * runs before it sends again. A refusal, the server's (a namespace that is not empty) or a
 * failed touch, stays in the dialog with its sentence. While the request is open, Escape and
 * Cancel do nothing.
 *
 * @param props - The namespace and the handlers.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace.
 * @param props.onDone - Called once it is deleted.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function DeleteNamespaceDialog({
  environment,
  namespace,
  onDone,
  onCancel,
}: DeleteNamespaceDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(environment);
  const [reason, setReason] = useState("");
  const needsReason = protectedEnvironment === true;
  const missing = needsReason && reason.trim() === "";
  const remove = (): void => {
    attempt.run(async () => {
      await deleteNamespace(environment, namespace, needsReason ? reason.trim() : null);
      onDone();
    });
  };
  const confirm = useConfirm({
    waiting: protectedEnvironment === undefined,
    disabled: missing,
    onConfirm: remove,
  });
  const submit = (): void => {
    if (!attempt.busy) {
      confirm.press();
    }
  };
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">
        {t("catalog.places.delete.title", { namespace: markedText(namespace) })}
      </Heading>
      <p>{t("catalog.places.delete.text")}</p>
      {needsReason && (
        <ProtectedReason
          environment={environment}
          value={reason}
          onChange={setReason}
          onSubmit={submit}
        />
      )}
      <ConfirmRow
        attempt={attempt}
        label={t("catalog.places.delete.confirm")}
        confirm={confirm}
        onCancel={close}
      />
    </ActionDialog>
  );
}
