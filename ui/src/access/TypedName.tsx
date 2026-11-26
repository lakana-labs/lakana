/**
 * The end of a dialog that deletes something only once its name is typed exactly: the field,
 * the held delete button with the sentence that says why, and Cancel; and whether what is typed
 * is that name.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { Button, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { PendingButton } from "../shell/PendingButton";
import { markedText } from "../shell/VisibleText";

/**
 * Whether what is typed is the name: exactly, or as the label shows it, with each hidden
 * character written as its mark (`U+202E`), which is the only way to type a name that holds
 * one. The request still names the real name.
 *
 * @param typed - What is typed.
 * @param name - The name, as stored.
 * @returns True when they match.
 */
export function typedMatches(typed: string, name: string): boolean {
  return typed === name || typed === markedText(name);
}

/** Properties of {@link TypedName}. */
export interface TypedNameProps {
  /** The field's label, naming what to type. */
  readonly label: string;
  /** What is typed. */
  readonly typed: string;
  /** Called as the person types. */
  readonly onTyped: (typed: string) => void;
  /** Whether what is typed is the name exactly. */
  readonly matches: boolean;
  /** The delete button's label; it submits the dialog's form. */
  readonly submit: string;
  /** The sentence that says why the button is held. */
  readonly held: string;
  /** Whether the delete is on its way. */
  readonly busy: boolean;
  /** Closes the dialog. */
  readonly onCancel: () => void;
}

/**
 * Renders the field, the delete button (held, and described by why, until the name is typed)
 * and Cancel. It sits inside the dialog's `Form`, whose submit does the delete.
 *
 * @param props - The labels, what is typed and the handlers.
 * @param props.label - The field's label.
 * @param props.typed - What is typed.
 * @param props.onTyped - Called as the person types.
 * @param props.matches - Whether what is typed is the name.
 * @param props.submit - The delete button's label.
 * @param props.held - Why the button is held.
 * @param props.busy - Whether the delete is on its way.
 * @param props.onCancel - Closes the dialog.
 * @returns The field and the buttons.
 */
export function TypedName({
  label,
  typed,
  onTyped,
  matches,
  submit,
  held,
  busy,
  onCancel,
}: TypedNameProps): JSX.Element {
  const { t } = useTranslation();
  const heldId = useId();
  return (
    <>
      <TextField className="field" value={typed} onChange={onTyped}>
        <Label>{label}</Label>
        <Input autoComplete="off" spellCheck={false} />
      </TextField>
      <div className="step-actions">
        <PendingButton
          type="submit"
          className="button button-primary"
          pending={busy}
          isDisabled={!matches}
          {...(matches ? {} : { "aria-describedby": heldId })}
        >
          {submit}
        </PendingButton>
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
      {!matches && (
        <p className="step-status" id={heldId}>
          {held}
        </p>
      )}
    </>
  );
}
