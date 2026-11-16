/**
 * The question before a drop: what dropping a table or an artifact does, then do it or cancel.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { dropArtifact, dropTable, type OrphanOut, type TableOut, type TableRef } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import "./actionStrings";
import { ConfirmRow, useConfirm } from "./ConfirmRow";
import { ProtectedReason } from "./ProtectedReason";
import { useProtected } from "./useProtected";

/** What a drop removes. */
interface DropTarget {
  /** The table, or the artifact by its name. */
  readonly ref: TableRef;
  /** Its kind; an orphan's may be unknown (null), which is dropped as a table. */
  readonly kind: OrphanOut["kind"] | TableOut["kind"];
}

/** Properties of {@link DropDialog}. */
export interface DropDialogProps {
  /** What to drop. */
  readonly target: DropTarget;
  /** Called once it is dropped; the caller decides where to go. */
  readonly onDone: () => void;
  /** Closes the dialog without dropping anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the question. A table can be restored from Dropped unless "Delete the files now" is
 * checked; an artifact cannot, and has no such box. In a protected environment it asks for the
 * reason to break the protection first. A refusal, the server's or a failed passkey touch,
 * stays in the dialog with its sentence. While the drop is pending, the dialog cannot be closed.
 *
 * @param props - What to drop and the handlers.
 * @param props.target - What to drop.
 * @param props.onDone - Called once it is dropped.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function DropDialog({ target, onDone, onCancel }: DropDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const { ref, kind } = target;
  const protectedEnvironment = useProtected(ref.environment);
  const [purge, setPurge] = useState(false);
  const [reason, setReason] = useState("");
  const artifact = kind === "ARTIFACT";
  const needsReason = protectedEnvironment === true;
  const drop = (): void => {
    attempt.run(async () => {
      const breakGlassReason = needsReason ? reason.trim() : null;
      if (artifact) {
        await dropArtifact(ref, breakGlassReason);
      } else {
        await dropTable(ref, { purge, breakGlassReason });
      }
      onDone();
    });
  };
  const confirm = useConfirm({
    waiting: protectedEnvironment === undefined,
    disabled: needsReason && reason.trim() === "",
    onConfirm: drop,
  });
  const submit = (): void => {
    if (!attempt.busy) {
      confirm.press();
    }
  };
  // While the drop or its passkey touch is pending, Escape, the close button and Cancel do nothing.
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">
        {t("table.actions.drop.title", { table: markedText(`${ref.namespace}.${ref.name}`) })}
      </Heading>
      {artifact ? (
        <p>{t("table.actions.drop.artifactText")}</p>
      ) : (
        <>
          <p>{t("table.actions.drop.text")}</p>
          <p>
            <label>
              <input
                type="checkbox"
                checked={purge}
                onChange={(event) => {
                  setPurge(event.target.checked);
                }}
              />{" "}
              {t("table.actions.drop.purge")}
            </label>
          </p>
        </>
      )}
      {needsReason && (
        <ProtectedReason
          environment={ref.environment}
          value={reason}
          onChange={setReason}
          onSubmit={submit}
        />
      )}
      <ConfirmRow
        attempt={attempt}
        label={t(artifact ? "table.actions.drop.artifactConfirm" : "table.actions.drop.confirm")}
        confirm={confirm}
        onCancel={close}
      />
    </ActionDialog>
  );
}
