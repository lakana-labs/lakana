/**
 * New service account: the name, then the account, after the passkey touch.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { FieldError, Form, Heading, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { createServiceAccount } from "../api/principals";
import { ActionDialog } from "../shell/ActionDialog";
import { useAttempt } from "../sign-in/Attempt";
import "./principalStrings";
import { FormFoot } from "./FormFoot";

/** Properties of {@link NewServiceAccountDialog}. */
export interface NewServiceAccountDialogProps {
  /** Called with the new account's name once it is created. */
  readonly onCreated: (name: string) => void;
  /** Closes the dialog without creating anything. */
  readonly onClose: () => void;
}

/**
 * Asks for the name and creates the service account, asking for the passkey touch first when the
 * session's is stale. Create is held, and says why, until a name is typed; the server's refusal
 * (a name taken, hidden characters, an instance an access file manages) stays in the dialog with
 * the name kept, and takes the focus. While the request is open, Escape and Cancel do nothing.
 *
 * @param props - The handlers.
 * @param props.onCreated - Called once the account exists.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function NewServiceAccountDialog({
  onCreated,
  onClose,
}: NewServiceAccountDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [name, setName] = useState("");
  const wanted = name.trim();
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (wanted === "" || attempt.busy) return;
          attempt.run(async () => {
            const created = await createServiceAccount(wanted);
            onCreated(created.name);
          });
        }}
      >
        <Heading slot="title">{t("access.serviceAccounts.create")}</Heading>
        <p>{t("access.serviceAccounts.intro")}</p>
        <TextField className="field" isRequired value={name} onChange={setName}>
          <Label>{t("access.serviceAccounts.name")}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t("access.serviceAccounts.nameHelp")}</Text>
          <FieldError />
        </TextField>
        <FormFoot
          attempt={attempt}
          submit={t("access.serviceAccounts.submit")}
          held={wanted === "" ? t("access.serviceAccounts.nameNeeded") : null}
          blocked={false}
          onCancel={close}
        />
      </Form>
    </ActionDialog>
  );
}
