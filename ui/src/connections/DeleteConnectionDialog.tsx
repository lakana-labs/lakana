/**
 * The question before a connection is deleted: or, while an asset still declares it, why it
 * cannot be.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { type ConnectionOut, deleteConnection } from "../api";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ActionDialog } from "../shell/ActionDialog";
import { markedText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "./connectionStrings";

/** Properties of {@link DeleteConnectionDialog}. */
export interface DeleteConnectionDialogProps {
  /** The connection to delete. */
  readonly connection: ConnectionOut;
  /** Called once it is deleted; the caller decides where to go. */
  readonly onDone: () => void;
  /** Closes the dialog without deleting anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the question. A connection that assets still declare is not deleted from here: the
 * dialog names the assets, says to remove it from them in git, and only closes; nothing is sent.
 * Otherwise it asks, says that the secret stays in the environment, unbound, and deletes once.
 * From the browser the server asks for a passkey touch, which the request runs before it sends
 * again. A refusal, such as the 409 of an asset that declared it meanwhile, stays in the dialog
 * with the server's sentence. While the request is open, Escape and Cancel do nothing.
 *
 * @param props - The connection and the handlers.
 * @param props.connection - The connection.
 * @param props.onDone - Called once it is deleted.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function DeleteConnectionDialog({
  connection,
  onDone,
  onCancel,
}: DeleteConnectionDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const { environment, name, secret, declared_by: declaredBy } = connection;
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: false,
    onConfirm: () => {
      attempt.run(async () => {
        await deleteConnection(environment, name);
        onDone();
      });
    },
  });
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  const secretLine = (
    <p>
      {secret === null
        ? t("connections.delete.noSecret")
        : t("connections.delete.secret", { secret: markedText(secret) })}
    </p>
  );
  if (declaredBy.length > 0) {
    return (
      <ActionDialog onClose={close}>
        <Heading slot="title">
          {t("connections.delete.inUseTitle", { name: markedText(name) })}
        </Heading>
        <ul className="connection-list">
          {declaredBy.map(({ repository, asset }) => (
            <li key={`${repository}\u0000${asset}`}>
              {t("connections.page.declared", {
                repository: markedText(repository),
                asset: markedText(asset),
              })}
            </li>
          ))}
        </ul>
        <p>{t("connections.delete.inUse")}</p>
        {secretLine}
        <div className="step-actions">
          <Button className="button button-primary" onPress={close}>
            {t("connections.delete.close")}
          </Button>
        </div>
      </ActionDialog>
    );
  }
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">
        {t("connections.delete.title", {
          name: markedText(name),
          environment: markedText(environment),
        })}
      </Heading>
      {secretLine}
      <ConfirmRow
        attempt={attempt}
        label={t("connections.delete.confirm")}
        confirm={confirm}
        onCancel={close}
      />
    </ActionDialog>
  );
}
