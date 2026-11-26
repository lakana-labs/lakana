/**
 * The question before an administrator deactivates a person or service account: what ends, what
 * stays, and that it cannot be undone in this release; the name typed exactly, then the
 * passkey touch.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Form, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { deactivatePrincipal } from "../api/principals";
import { ActionDialog } from "../shell/ActionDialog";
import { useAttempt } from "../sign-in/Attempt";
import "./principalStrings";
import { TypedName } from "./TypedName";

/** Whose account is deactivated, as far as what ends goes. */
export type DeactivatedKind = "own" | "person" | "service";

/** Properties of {@link DeactivateDialog}. */
export interface DeactivateDialogProps {
  /** The principal name. */
  readonly name: string;
  /** Whether it is one's own account, another person's or a service account. */
  readonly kind: DeactivatedKind;
  /** Called once the principal is deactivated. */
  readonly onDone: () => void;
  /** Closes the dialog without deactivating; focus returns to the button that opened it. */
  readonly onCancel: () => void;
}

/**
 * Renders the dialog. Deactivate is held, and says why, until the name is typed exactly; it asks
 * for the passkey touch first when the session's is stale. A refusal (the last administrator,
 * an instance an access file manages) stays in the dialog in the server's words and takes the
 * focus; for an account without a passkey it links to where to add one. While the request is
 * open, Escape and Cancel do nothing.
 *
 * @param props - The principal, its kind and the handlers.
 * @param props.name - The principal name.
 * @param props.kind - Whose account it is.
 * @param props.onDone - Called once it is deactivated.
 * @param props.onCancel - Closes without deactivating.
 * @returns The dialog.
 */
export function DeactivateDialog({
  name,
  kind,
  onDone,
  onCancel,
}: DeactivateDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const alert = useRef<HTMLParagraphElement>(null);
  const matches = typed === name;
  useEffect(() => {
    if (attempt.error !== null) alert.current?.focus();
  }, [attempt.error]);
  const close = (): void => {
    if (!attempt.busy) onCancel();
  };
  const ends = {
    own: "access.deactivate.endsOwn",
    person: "access.deactivate.endsPerson",
    service: "access.deactivate.endsService",
  }[kind];
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (!matches || attempt.busy) return;
          attempt.run(async () => {
            await deactivatePrincipal(name);
            onDone();
          });
        }}
      >
        <Heading slot="title">
          {kind === "own"
            ? t("access.deactivate.dialogTitleOwn")
            : t("access.deactivate.dialogTitle", { name })}
        </Heading>
        <p>{t(ends, { name })}</p>
        <p>{t("access.deactivate.kept")}</p>
        <p>{t("access.deactivate.final", { name })}</p>
        <TypedName
          label={t("access.deactivate.type", { name })}
          typed={typed}
          onTyped={setTyped}
          matches={matches}
          submit={t("access.deactivate.action")}
          held={t("access.deactivate.held")}
          busy={attempt.busy}
          onCancel={close}
        />
        <p className="step-status" role="status">
          {attempt.busy ? t("common.working") : ""}
        </p>
        {attempt.error !== null && (
          <p className="step-error" role="alert" tabIndex={-1} ref={alert}>
            {attempt.error}
            {attempt.addPasskey && (
              <>
                {" "}
                <Link to="/account">{t("passkey.add")}</Link>
              </>
            )}
          </p>
        )}
      </Form>
    </ActionDialog>
  );
}
