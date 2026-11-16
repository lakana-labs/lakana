/**
 * The form that creates a namespace, as `lakana ns create` does.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Heading, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { createNamespace } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { ConfirmRow } from "../actions/ConfirmRow";
import { ProtectedReason } from "../actions/ProtectedReason";
import { useProtected } from "../actions/useProtected";
import "./placeStrings";

/** Properties of {@link NewNamespaceDialog}. */
export interface NewNamespaceDialogProps {
  /** The environment the namespace is created in. */
  readonly environment: string;
  /** The namespace the menu was opened on, whose path the field starts with; null for none. */
  readonly parent: string | null;
  /** Called with the created path; the caller decides where to go. */
  readonly onDone: (path: string) => void;
  /** Closes the dialog without creating anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the form: the path, which starts with the parent and a dot, and in a protected
 * environment the reason to break the protection. The namespace is created with no properties;
 * they are set from the CLI. The server judges the path; its refusal stays in the dialog, word
 * for word, with the path as typed. While the request is open, Escape and Cancel do nothing.
 *
 * @param props - The place and the handlers.
 * @param props.environment - The environment.
 * @param props.parent - The namespace the menu was opened on; null for none.
 * @param props.onDone - Called with the created path.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function NewNamespaceDialog({
  environment,
  parent,
  onDone,
  onCancel,
}: NewNamespaceDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(environment);
  const [path, setPath] = useState(parent === null ? "" : `${parent}.`);
  const [reason, setReason] = useState("");
  const needsReason = protectedEnvironment === true;
  const missing = path.trim() === "" || (needsReason && reason.trim() === "");
  const ready = !missing && protectedEnvironment !== undefined && !attempt.busy;
  const create = (): void => {
    const sent = path.trim();
    attempt.run(async () => {
      const created = await createNamespace(environment, sent, needsReason ? reason.trim() : null);
      onDone(created.path);
    });
  };
  const submit = (): void => {
    if (ready) {
      create();
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
        {t("catalog.places.create.title", { place: markedText(parent ?? environment) })}
      </Heading>
      <TextField
        className="field"
        value={path}
        onChange={setPath}
        isRequired
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            submit();
          }
        }}
      >
        <Label>{t("catalog.places.create.path")}</Label>
        <Input />
        <Text slot="description">{t("catalog.places.create.pathText")}</Text>
      </TextField>
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
        label={t("catalog.places.create.confirm")}
        waiting={protectedEnvironment === undefined}
        disabled={missing}
        onConfirm={create}
        onCancel={close}
      />
    </ActionDialog>
  );
}
