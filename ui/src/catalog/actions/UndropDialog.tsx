/**
 * The dialog that restores a dropped table when it needs more than a press: a reason in a
 * protected environment, or another name when the table's own is taken.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Heading, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ApiError, type TableOut } from "../../api";
import { type DroppedTableOut, undropTable } from "../../api/manage";
import { ActionDialog } from "../../shell/ActionDialog";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import "./actionStrings";
import { ConfirmRow } from "./ConfirmRow";
import { ProtectedReason } from "./ProtectedReason";
import { useProtected } from "./useProtected";

/** Properties of {@link UndropDialog}. */
export interface UndropDialogProps {
  /** The dropped table. */
  readonly row: DroppedTableOut;
  /** The environment it was dropped from. */
  readonly environment: string;
  /** Called with the restored table once it is back. */
  readonly onDone: (restored: TableOut) => void;
  /** Closes the dialog without restoring anything. */
  readonly onCancel: () => void;
  /** The server's sentence when the table's name is taken; null when it is not known to be. */
  readonly nameTaken: string | null;
}

/**
 * Renders the dialog. It reads whether the environment is protected and then asks for a reason;
 * when the name is taken (known on opening, or learned from the server's answer) it shows that
 * sentence and asks for another name. A refusal stays in the dialog with its cause.
 *
 * @param props - The table, the environment and the handlers.
 * @param props.row - The dropped table.
 * @param props.environment - The environment it was dropped from.
 * @param props.onDone - Called with the restored table.
 * @param props.onCancel - Closes the dialog.
 * @param props.nameTaken - The server's sentence when the name is taken.
 * @returns The dialog.
 */
export function UndropDialog({
  row,
  environment,
  onDone,
  onCancel,
  nameTaken,
}: UndropDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(environment);
  const [reason, setReason] = useState("");
  const [name, setName] = useState("");
  const [taken, setTaken] = useState(nameTaken);
  const needsReason = protectedEnvironment === true;
  const missing = (needsReason && reason.trim() === "") || (taken !== null && name.trim() === "");

  const restore = (): void => {
    attempt.run(async () => {
      try {
        onDone(
          await undropTable(
            { environment, namespace: row.namespace, name: row.name },
            {
              newName: taken === null ? null : name.trim(),
              breakGlassReason: needsReason ? reason.trim() : null,
            },
          ),
        );
      } catch (error) {
        if (taken === null && error instanceof ApiError && error.type === "AlreadyExistsError") {
          setTaken(error.detail);
          return;
        }
        throw error;
      }
    });
  };

  return (
    <ActionDialog onClose={onCancel}>
      <Heading slot="title">
        {t("table.actions.undrop.title", { table: markedText(`${row.namespace}.${row.name}`) })}
      </Heading>
      {taken === null ? (
        <p>{t("table.actions.undrop.text")}</p>
      ) : (
        <>
          <p role="alert">{taken}</p>
          <TextField className="field" value={name} onChange={setName} isRequired>
            <Label>{t("table.actions.undrop.restoreAs")}</Label>
            <Input />
            <Text slot="description">
              {t("table.actions.undrop.restoreAsHelp", { namespace: markedText(row.namespace) })}
            </Text>
          </TextField>
        </>
      )}
      {needsReason && (
        <ProtectedReason environment={environment} value={reason} onChange={setReason} />
      )}
      <ConfirmRow
        attempt={attempt}
        label={t("table.actions.undrop.confirm")}
        waiting={protectedEnvironment === undefined}
        disabled={missing}
        onConfirm={restore}
        onCancel={onCancel}
      />
    </ActionDialog>
  );
}
