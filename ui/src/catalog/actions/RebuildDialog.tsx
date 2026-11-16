/**
 * The question before a rebuild from git, and what the rebuild did: the run that builds the
 * table again and the command to follow it.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Heading, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { rebuildTable, type TableRebuildOut, type TableRef } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { CopyLine } from "../../shell/CopyLine";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { shellQuote } from "../../shell/shellQuote";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { catalogPath } from "../paths";
import { DEFAULT_VIEW } from "../view";
import "./actionStrings";
import { ConfirmRow, useConfirm } from "./ConfirmRow";
import { useProtected } from "./useProtected";

/** The most the server takes as a reason. */
const REASON_MAX = 500;

/** Properties of {@link RebuildDialog}. */
export interface RebuildDialogProps {
  /** The table. */
  readonly place: TableRef;
  /** Closes the dialog before anything was rebuilt. */
  readonly onClose: () => void;
}

/**
 * Renders the question with the reason the audit log records, then, once the server queued the
 * run, the sentence that names it and the command to follow it. In a protected environment the
 * reason is also the reason to break the protection, as with `--break-glass`, and the dialog
 * says so. Closing after the rebuild goes to the table's place in the catalog, because the
 * table is dropped until its run builds it again.
 *
 * @param props - The table and the close handler.
 * @param props.place - The table.
 * @param props.onClose - Closes the dialog before anything was rebuilt.
 * @returns The dialog.
 */
export function RebuildDialog({ place, onClose }: RebuildDialogProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(place.environment);
  const [reason, setReason] = useState("");
  const [rebuilt, setRebuilt] = useState<TableRebuildOut | null>(null);
  const { target: doneLine, arm: armFocus } = useFocusAfter<HTMLParagraphElement>(rebuilt);
  const { environment, namespace, name } = place;
  const table = markedText(`${namespace}.${name}`);
  const toCatalog = (): void => {
    void navigate(catalogPath({ ...DEFAULT_VIEW, environment, namespace }));
  };
  const rebuild = (): void => {
    attempt.run(async () => {
      const answer = await rebuildTable(place, reason.trim(), protectedEnvironment === true);
      armFocus();
      setRebuilt(answer);
    });
  };
  const confirm = useConfirm({
    waiting: protectedEnvironment === undefined,
    disabled: reason.trim() === "",
    onConfirm: rebuild,
  });
  // While the request is open Escape does nothing: closing then would lose the run it queues.
  const close = rebuilt === null ? onClose : toCatalog;
  return (
    <ActionDialog
      onClose={() => {
        if (!attempt.busy) {
          close();
        }
      }}
    >
      <Heading slot="title">{t("table.actions.rebuild.title", { table })}</Heading>
      {rebuilt === null ? (
        <>
          <p>{t("table.actions.rebuild.text")}</p>
          {protectedEnvironment === true && (
            <p>{t("table.actions.rebuild.protected", { environment })}</p>
          )}
          <TextField
            className="field"
            value={reason}
            onChange={setReason}
            isRequired
            maxLength={REASON_MAX}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                // Kept like a click while the protection is not known yet.
                if (!attempt.busy) {
                  confirm.press();
                }
              }
            }}
          >
            <Label>{t("table.actions.rebuild.reason")}</Label>
            <Input />
          </TextField>
          <ConfirmRow
            attempt={attempt}
            label={t("table.actions.rebuild.confirm")}
            confirm={confirm}
            onCancel={onClose}
          />
        </>
      ) : (
        <>
          <p ref={doneLine} tabIndex={-1}>
            {t(
              rebuilt.path === null
                ? "table.actions.rebuild.doneNoPath"
                : "table.actions.rebuild.done",
              {
                table,
                run: rebuilt.run_id,
                repository: markedText(rebuilt.repository),
                path: markedText(rebuilt.path ?? ""),
              },
            )}
          </p>
          <CopyLine
            text={`lakana runs show ${shellQuote(rebuilt.run_id)} --env ${shellQuote(environment)}`}
            copyLabel={t("table.actions.rebuild.copy")}
          />
          <div className="step-actions">
            <Button className="button button-primary" onPress={toCatalog}>
              {t("table.actions.rebuild.close")}
            </Button>
          </div>
        </>
      )}
    </ActionDialog>
  );
}
