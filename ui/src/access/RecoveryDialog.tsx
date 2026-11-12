/**
 * The dialog that issues a recovery link: for a person who is not an administrator, the link,
 * shown once; for an administrator, the server operator's command instead.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, FieldError, Form, Heading, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { createRecoveryLink, isAdministrator, type RecoveryLinkOut } from "../api";
import { ActionDialog } from "../shell/ActionDialog";
import { CopyLine } from "../shell/CopyLine";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { ShownOnce } from "./ShownOnce";
import { PendingButton } from "../shell/PendingButton";

/** Properties of {@link RecoveryDialog}. */
export interface RecoveryDialogProps {
  /** Whose account; when left out, the dialog asks for the name. */
  readonly name?: string;
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/** Where the dialog is: asking, showing the operator's command, or showing the link. */
type Stage =
  | { readonly kind: "ask" }
  | { readonly kind: "operator" }
  | { readonly kind: "issued"; readonly link: RecoveryLinkOut };

/**
 * Asks for whose account (unless named), finds out whether the person is an administrator, and
 * then either shows the operator's command or issues the link (a passkey touch when the server
 * asks for one) and shows it once.
 *
 * @param props - The name and the close handler.
 * @param props.name - Whose account, if already known.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function RecoveryDialog({ name: known, onClose }: RecoveryDialogProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const attempt = useAttempt();
  const [name, setName] = useState(known ?? "");
  const [stage, setStage] = useState<Stage>({ kind: "ask" });
  const done = (
    <div className="step-actions">
      {/* The Continue button that had focus is gone; focus lands on the way out. */}
      {/* eslint-disable-next-line jsx-a11y/no-autofocus */}
      <Button className="button button-primary" onPress={onClose} autoFocus>
        {t("dialog.done")}
      </Button>
    </div>
  );
  return (
    <ActionDialog onClose={onClose}>
      {stage.kind === "ask" && (
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            attempt.run(async () => {
              setStage(
                (await isAdministrator(name))
                  ? { kind: "operator" }
                  : { kind: "issued", link: await createRecoveryLink(name) },
              );
            });
          }}
        >
          <Heading slot="title">{t("access.recovery.action")}</Heading>
          <p>{t("access.recovery.intro")}</p>
          {known === undefined ? (
            <TextField className="field" isRequired value={name} onChange={setName}>
              <Label>{t("access.invites.name")}</Label>
              <Input />
              <FieldError />
            </TextField>
          ) : (
            <p>{t("access.recovery.askFor", { name: known })}</p>
          )}
          <p className="page-notice">{t("access.recovery.confirmFirst")}</p>
          <div className="step-actions">
            <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
              {t("access.recovery.continue")}
            </PendingButton>
            <Button className="button" onPress={onClose}>
              {t("dialog.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("common.working")} />
        </Form>
      )}
      {stage.kind === "operator" && (
        <>
          <Heading slot="title">{t("access.recovery.adminTitle", { name })}</Heading>
          <p>{t("access.recovery.operatorOnly")}</p>
          <CopyLine
            text={t("access.recovery.command", { name })}
            copyLabel={t("comingSoon.copy")}
          />
          {done}
        </>
      )}
      {stage.kind === "issued" && (
        <ShownOnce
          title={t("access.shownTitle", { name: stage.link.name })}
          url={stage.link.url}
          onDone={onClose}
        >
          <p>
            {t("access.recovery.shownOnce", {
              name: stage.link.name,
              date: when(stage.link.expires_at),
            })}
          </p>
        </ShownOnce>
      )}
    </ActionDialog>
  );
}
