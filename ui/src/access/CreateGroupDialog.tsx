/**
 * Create group: the name, then the group. It needs no passkey touch.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  FieldError,
  Form,
  Heading,
  Input,
  Label,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { createGroup } from "../api/groups";
import { ActionDialog } from "../shell/ActionDialog";
import { PendingButton } from "../shell/PendingButton";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import "./accessStrings";

/** Properties of {@link CreateGroupDialog}. */
export interface CreateGroupDialogProps {
  /** Called with the new group's name once it is created. */
  readonly onCreated: (name: string) => void;
  /** Closes the dialog without creating anything. */
  readonly onClose: () => void;
}

/**
 * Asks for the name and creates the group; the server's refusal (a name taken) stays in the
 * dialog with the name kept. While the request is open, Escape and Cancel do nothing.
 *
 * @param props - The handlers.
 * @param props.onCreated - Called once the group exists.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function CreateGroupDialog({ onCreated, onClose }: CreateGroupDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [name, setName] = useState("");
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          attempt.run(async () => {
            const created = await createGroup(name.trim());
            onCreated(created.name);
          });
        }}
      >
        <Heading slot="title">{t("access.groups.create")}</Heading>
        <TextField className="field" isRequired value={name} onChange={setName}>
          <Label>{t("access.groups.name")}</Label>
          <Input />
          <Text slot="description">{t("access.groups.nameHelp")}</Text>
          <FieldError />
        </TextField>
        <div className="step-actions">
          <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
            {t("access.groups.createConfirm")}
          </PendingButton>
          <Button className="button" onPress={close}>
            {t("dialog.cancel")}
          </Button>
        </div>
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}
