/**
 * What the controls of a job and a run share: the line that says what a control did or why the
 * server refused it, the button that acts at once, and the question before a control that asks
 * first.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ActionDialog } from "../shell/ActionDialog";
import { PendingButton } from "../shell/PendingButton";
import { VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import "./jobStrings";

/** What a control did: its kind, its sentence and, for a waiting start, the run it waits for. */
export interface ControlNoteValue {
  /** What happened; `refused` is the server's refusal. */
  readonly kind: "started" | "waits" | "cancelling" | "paused" | "resumed" | "refused";
  /** The sentence, already in words. */
  readonly text: string;
  /** The page of the run a waiting start waits for. */
  readonly to?: string;
}

/** Properties of {@link ControlNote}. */
export interface ControlNoteProps {
  /** What happened; null keeps an empty status line, so the next note is announced. */
  readonly note: ControlNoteValue | null;
}

/**
 * Renders the note as a status line, a refusal as an alert, with the link to the run a waiting
 * start waits for. The sentence may hold a name or the server's words, so hidden characters show
 * as marks.
 *
 * @param props - The note.
 * @param props.note - What happened.
 * @returns The line.
 */
export function ControlNote({ note }: ControlNoteProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <p className="jobs-note" role={note?.kind === "refused" ? "alert" : "status"}>
      {note !== null && (
        <>
          <VisibleText text={note.text} />
          {note.to !== undefined && (
            <>
              {" "}
              <Link to={note.to}>{t("jobs.note.waitsFor")}</Link>
            </>
          )}
        </>
      )}
    </p>
  );
}

/** Properties of {@link ControlButton}: it acts at once, or it asks first. */
export type ControlButtonProps = {
  /** The button's label. */
  readonly label: string;
  /** Whether it is the page's one primary action; no by default. */
  readonly primary?: boolean;
} & (
  | {
      /** Acts at once; a refusal shows the server's sentence under the button. */
      readonly act: () => Promise<void>;
      /** Never set beside `act`. */
      readonly ask?: undefined;
    }
  | {
      /** Asks first instead of acting, such as by opening a question. */
      readonly ask: () => void;
      /** Never set beside `ask`. */
      readonly act?: undefined;
    }
);

/**
 * A control that acts at once: the pending sign and "Working…" while the request is open, the
 * focus kept on the button, and the server's refusal under it as an alert, the button still
 * usable.
 *
 * @param props - The label, the action and whether it asks first.
 * @param props.label - The button's label.
 * @param props.primary - Whether it is the page's one primary action.
 * @param props.act - Acts at once.
 * @param props.ask - Asks first instead.
 * @returns The button with its refusal.
 */
export function ControlButton({
  label,
  primary = false,
  act,
  ask,
}: ControlButtonProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  return (
    <span className="jobs-control">
      <PendingButton
        className={primary ? "button button-primary" : "button"}
        pending={attempt.busy}
        onPress={() => {
          if (ask !== undefined) {
            ask();
          } else {
            attempt.run(act);
          }
        }}
      >
        {label}
      </PendingButton>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
    </span>
  );
}

/** Properties of {@link AskDialog}. */
export interface AskDialogProps {
  /** The question, which names the job or the run. */
  readonly title: string;
  /** What happens, and what stays. */
  readonly text: string;
  /** The confirm button's label. */
  readonly label: string;
  /** Acts once confirmed. */
  readonly act: () => Promise<void>;
  /** Closes the question, after the action or without it. */
  readonly onClose: () => void;
}

/**
 * The question before a control that asks first. Its confirm button sends one request and closes
 * the question; a refusal stays in it with the server's sentence. While the request is open,
 * Escape and Cancel do nothing.
 *
 * @param props - The question, its words and the action.
 * @param props.title - The question.
 * @param props.text - What happens, and what stays.
 * @param props.label - The confirm button's label.
 * @param props.act - Acts once confirmed.
 * @param props.onClose - Closes the question.
 * @returns The dialog.
 */
export function AskDialog({ title, text, label, act, onClose }: AskDialogProps): JSX.Element {
  const attempt = useAttempt();
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: false,
    onConfirm: () => {
      attempt.run(async () => {
        await act();
        onClose();
      });
    },
  });
  const close = (): void => {
    if (!attempt.busy) {
      onClose();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">{title}</Heading>
      <p>{text}</p>
      <ConfirmRow attempt={attempt} label={label} confirm={confirm} onCancel={close} />
    </ActionDialog>
  );
}
