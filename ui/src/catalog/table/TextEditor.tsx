/**
 * The editor of a table's or column's label and description.
 *
 * @packageDocumentation
 */
import {
  type JSX,
  type KeyboardEvent,
  type RefObject,
  type SyntheticEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button, Input, Label, Text, TextArea, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ApiError, describeColumn, describeTable, type TableOut, type TextChange } from "../../api";
import { PendingButton } from "../../shell/PendingButton";
import { useAttempt } from "../../sign-in/Attempt";
import { problemText } from "../../sign-in/messages";
import { editLanguage, languageName } from "./language";
import { useTableData } from "./TableContext";

/** The most characters a description may have. */
export const DESCRIPTION_MAX = 4000;

/** The most characters a label may have. */
export const LABEL_MAX = 100;

/** The share of a limit after which the person is shown how much is used. */
const NEAR = 0.9;

/** Properties of {@link TextEditor}. */
export interface TextEditorProps {
  /** The texts as they are now; null for one there is none of. */
  readonly texts: { readonly description: string | null; readonly label: string | null };
  /** The language the texts are written in. */
  readonly lang: string;
  /** Whether to name the language beside the fields, because the table has more than one. */
  readonly named: boolean;
  /** Saves what changed; rejects with the server's refusal. */
  readonly onSave: (change: TextChange) => Promise<void>;
  /** Closes the editor, after a save, a cancel or nothing to save. */
  readonly onDone: () => void;
}

/** Which field a refusal belongs to, and its words. */
interface Refusal {
  /** The field the words are shown next to; `form` when no one field is meant. */
  readonly field: "label" | "description" | "form";
  /** What went wrong and what to do next. */
  readonly message: string;
}

/**
 * Counts characters the way the server does, a character outside the basic plane once.
 *
 * @param text - The text.
 * @returns How many characters it has.
 */
function characters(text: string): number {
  return Array.from(text).length;
}

/**
 * Finds the field a refusal is about: the one the server's sentence names, else the only one
 * that was sent, else none, because the sentence does not say which of two it means.
 *
 * @param message - The refusal's words.
 * @param change - What was sent.
 * @returns The field, or `form` when it cannot be told.
 */
function refusedField(message: string, change: TextChange): Refusal["field"] {
  const named = (["label", "description"] as const).filter((field) =>
    message.toLowerCase().includes(field),
  );
  if (named.length === 1) {
    return named[0] as "label" | "description";
  }
  if (change.label === undefined) {
    return "description";
  }
  return change.description === undefined ? "label" : "form";
}

/**
 * Renders the label and the description as fields with Save and Cancel. It sends only what
 * changed, refuses a blank or too long text before sending, keeps the text when the server
 * refuses, and sends one request however often Save is pressed. Escape cancels, unless a save is
 * open. Focus starts in the description; the caller moves it when the editor closes.
 *
 * @param props - The texts and what to do with them.
 * @param props.texts - The texts as they are now.
 * @param props.lang - The language they are written in.
 * @param props.named - Whether to name the language.
 * @param props.onSave - Saves what changed.
 * @param props.onDone - Closes the editor.
 * @returns The editor.
 */
export function TextEditor({ texts, lang, named, onSave, onDone }: TextEditorProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const [description, setDescription] = useState(texts.description ?? "");
  const [label, setLabel] = useState(texts.label ?? "");
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const sending = useRef(false);
  const start = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    start.current?.focus();
  }, []);
  const number = new Intl.NumberFormat(i18n.language);
  const language = named ? languageName(lang, i18n.language) : null;
  const name = (key: "label" | "description"): string =>
    language === null ? t(`table.edit.${key}`) : t(`table.edit.${key}In`, { language });
  const descriptionUsed = characters(description.trim());
  const labelUsed = characters(label.trim());

  /**
   * Checks the fields and the changes, then saves them.
   *
   * @param event - The form's submission.
   */
  function submit(event: SyntheticEvent): void {
    event.preventDefault();
    if (sending.current) {
      return;
    }
    const change: { description?: string; label?: string } = {};
    if (description !== (texts.description ?? "")) {
      change.description = description;
    }
    if (label !== (texts.label ?? "")) {
      change.label = label;
    }
    const checks = [
      ["description", change.description, DESCRIPTION_MAX, descriptionUsed],
      ["label", change.label, LABEL_MAX, labelUsed],
    ] as const;
    for (const [field, sent, max, used] of checks) {
      const problem =
        sent === undefined
          ? null
          : sent.trim() === ""
            ? t(field === "label" ? "table.edit.blankLabel" : "table.edit.blankDescription")
            : used > max
              ? t(field === "label" ? "table.edit.tooLongLabel" : "table.edit.tooLongDescription", {
                  used: number.format(used),
                  max: number.format(max),
                })
              : null;
      if (problem !== null) {
        setRefusal({ field, message: problem });
        return;
      }
    }
    if (change.description === undefined && change.label === undefined) {
      onDone();
      return;
    }
    sending.current = true;
    setPending(true);
    setRefusal(null);
    onSave(change).then(onDone, (failure: unknown) => {
      sending.current = false;
      setPending(false);
      const message = problemText(t, failure);
      setRefusal({ field: refusedField(message, change), message });
    });
  }

  /**
   * Cancels on Escape, unless a save is open.
   *
   * @param event - The key press.
   */
  function leave(event: KeyboardEvent): void {
    if (event.key === "Escape" && !event.nativeEvent.isComposing && !sending.current) {
      onDone();
    }
  }

  /**
   * How much of a limit is used, once it is near.
   *
   * @param used - The characters used.
   * @param max - The limit.
   * @returns The words, or an empty string while far from the limit.
   */
  function count(used: number, max: number): string {
    return used > max * NEAR
      ? t("table.edit.count", { used: number.format(used), max: number.format(max) })
      : "";
  }

  return (
    // Escape from any control in the form cancels it; the form itself is not a control.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <form
      className="text-editor"
      aria-label={t("table.edit.formLabel")}
      onSubmit={submit}
      onKeyDown={leave}
    >
      <TextField
        className="field"
        value={label}
        onChange={setLabel}
        isInvalid={refusal?.field === "label"}
      >
        <Label>{name("label")}</Label>
        <Input />
        <Text slot="description">{t("table.edit.labelHelp")}</Text>
        {refusal?.field === "label" && (
          <Text slot="errorMessage" role="alert">
            {refusal.message}
          </Text>
        )}
        <p className="second" role="status" aria-label={name("label")}>
          {count(labelUsed, LABEL_MAX)}
        </p>
      </TextField>
      <TextField
        className="field"
        value={description}
        onChange={setDescription}
        isInvalid={refusal?.field === "description"}
      >
        <Label>{name("description")}</Label>
        <TextArea rows={6} ref={start} />
        <p className="second" role="status" aria-label={name("description")}>
          {count(descriptionUsed, DESCRIPTION_MAX)}
        </p>
        {refusal?.field === "description" && (
          <Text slot="errorMessage" role="alert">
            {refusal.message}
          </Text>
        )}
      </TextField>
      {refusal?.field === "form" && (
        <p className="step-error" role="alert">
          {refusal.message}
        </p>
      )}
      <div className="text-editor-actions">
        <PendingButton type="submit" className="button button-primary" pending={pending}>
          {t("table.edit.save")}
        </PendingButton>
        <Button className="button" onPress={onDone} isDisabled={pending}>
          {t("table.edit.cancel")}
        </Button>
      </div>
    </form>
  );
}

/** What {@link useDescribe} hands back. */
export interface Describe {
  /** The language the edit is written in. */
  readonly lang: string;
  /** Whether to name the language beside the fields. */
  readonly named: boolean;
  /** Whether the page shows the main language for want of a text in the reader's. */
  readonly fallback: boolean;
  /**
   * Changes the table's own texts and shows the table the server answers.
   *
   * @param change - The fields to change.
   */
  readonly saveTable: (change: TextChange) => Promise<void>;
  /**
   * Changes one column's texts and shows the table the server answers.
   *
   * @param column - The column's name.
   * @param change - The fields to change.
   */
  readonly saveColumn: (column: string, change: TextChange) => Promise<void>;
}

/**
 * Saves texts of the table the page shows, in the language of the text being edited. The server
 * answers the whole table and the page shows it; when two people save at once the later save
 * wins, and the page shows what the server holds after it. A refusal for permission makes the
 * page read what the person may do again, so Edit goes away for someone who no longer may.
 *
 * @param descriptionLanguage - The language of the description shown; null without one.
 * @returns The language and the two ways to save.
 */
export function useDescribe(descriptionLanguage: string | null): Describe {
  const { ref, table, replaceTable, permissions } = useTableData();
  const { reload } = permissions;
  const { lang, named, fallback } = editLanguage(table, {
    description_language: descriptionLanguage,
  });
  const keep = useCallback(
    async (save: Promise<TableOut>): Promise<void> => {
      try {
        replaceTable(await save);
      } catch (failure) {
        if (failure instanceof ApiError && failure.status === 403) {
          reload();
        }
        throw failure;
      }
    },
    [replaceTable, reload],
  );
  const saveTable = useCallback(
    (change: TextChange) => keep(describeTable(ref, change, lang)),
    [keep, ref, lang],
  );
  const saveColumn = useCallback(
    (column: string, change: TextChange) => keep(describeColumn(ref, column, change, lang)),
    [keep, ref, lang],
  );
  return { lang, named, fallback, saveTable, saveColumn };
}

/** What {@link useReturnFocus} hands back. */
export interface ReturnFocus<F extends HTMLElement> {
  /** The Edit button's ref. */
  readonly edit: RefObject<HTMLButtonElement | null>;
  /** The place to focus when there is no Edit button any more; it needs `tabIndex={-1}`. */
  readonly fallback: RefObject<F | null>;
  /** Asks for focus once the next render has shown the editor closed. */
  readonly returned: () => void;
}

/**
 * Puts focus back where the person was after the editor closes or a text is reset: on Edit, or,
 * when Edit is gone (the person may no longer edit), on the fallback. Without it focus would
 * fall to the page when the pressed button goes away.
 *
 * @template F - The element type of the fallback.
 * @returns The refs and the way to ask.
 */
export function useReturnFocus<F extends HTMLElement>(): ReturnFocus<F> {
  const edit = useRef<HTMLButtonElement>(null);
  const fallback = useRef<F>(null);
  const [asked, setAsked] = useState(0);
  useEffect(() => {
    if (asked > 0) {
      (edit.current ?? fallback.current)?.focus();
    }
  }, [asked]);
  const returned = useCallback(() => {
    setAsked((count) => count + 1);
  }, []);
  return { edit, fallback, returned };
}

/** Properties of {@link BackToGit}. */
export interface BackToGitProps {
  /** What the text belongs to, to tell the buttons of a list apart; null for the page's own. */
  readonly name: string | null;
  /** Removes the edit; rejects with the server's refusal. */
  readonly reset: () => Promise<void>;
  /** Runs after the edit is removed. */
  readonly onDone: () => void;
}

/**
 * The button that removes an edit, so that the text from the project's Git repository shows
 * again, with its pending sign, and the cause of a refusal beside it.
 *
 * @param props - What to remove and what to do afterwards.
 * @param props.name - What the text belongs to.
 * @param props.reset - Removes the edit.
 * @param props.onDone - Runs afterwards.
 * @returns The button and, after a refusal, its cause.
 */
export function BackToGit({ name, reset, onDone }: BackToGitProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  return (
    <>
      <PendingButton
        className="button"
        pending={attempt.busy}
        {...(name === null ? {} : { "aria-label": t("table.edit.backFor", { name }) })}
        onPress={() => {
          attempt.run(async () => {
            await reset();
            onDone();
          });
        }}
      >
        {t("table.edit.back")}
      </PendingButton>
      {attempt.error !== null && (
        <p className="step-error" role="alert">
          {attempt.error}
        </p>
      )}
    </>
  );
}
