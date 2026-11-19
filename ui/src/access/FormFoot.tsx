/**
 * The foot of Access's forms that create something after the passkey touch: the submit button
 * (held, saying why, until the form is complete), Cancel, the wait and the refusal.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { PendingButton } from "../shell/PendingButton";
import type { Attempt } from "../sign-in/Attempt";

/** Properties of {@link FormFoot}. */
export interface FormFootProps {
  /** The form's attempt to send. */
  readonly attempt: Attempt;
  /** The submit button's words. */
  readonly submit: string;
  /** What the form still needs, or null when it is complete. */
  readonly held: string | null;
  /** Whether nothing may be sent at all (an access file manages it): no submit button. */
  readonly blocked: boolean;
  /** Closes the form. */
  readonly onCancel: () => void;
}

/**
 * Renders the submit button and Cancel, what the form still needs (the button's description),
 * the wait in words, and the refusal, which takes the focus; for an account without a passkey
 * it links to where to add one.
 *
 * @param props - The attempt, the words, the state and the handler.
 * @param props.attempt - The form's attempt.
 * @param props.submit - The submit button's words.
 * @param props.held - What the form still needs.
 * @param props.blocked - Whether nothing may be sent at all.
 * @param props.onCancel - Closes the form.
 * @returns The foot.
 */
export function FormFoot({ attempt, submit, held, blocked, onCancel }: FormFootProps): JSX.Element {
  const { t } = useTranslation();
  const heldId = useId();
  const alert = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (attempt.error !== null) alert.current?.focus();
  }, [attempt.error]);
  return (
    <>
      <div className="step-actions">
        {!blocked && (
          <PendingButton
            type="submit"
            className="button button-primary"
            pending={attempt.busy}
            isDisabled={held !== null}
            {...(held === null ? {} : { "aria-describedby": heldId })}
          >
            {submit}
          </PendingButton>
        )}
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
      {!blocked && held !== null && (
        <p className="step-status" id={heldId}>
          {held}
        </p>
      )}
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
    </>
  );
}
