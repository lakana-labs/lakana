/**
 * A text field that suggests the names of things that exist as one types: the principals of
 * a group's Add a member and the tokens' Owner; the principals or groups of Add grant's and Add
 * filter or mask's Name and of the Who filter of Grants and Row filters & masks; the tables of
 * Add filter or mask and of Row filters & masks' Table filter. The server
 * filters, under the person's own rights, so a name they may not see is never suggested; the
 * suggestion is help, and the server still checks what is sent.
 *
 * @packageDocumentation
 */
import { type JSX, type Ref, useCallback, useEffect, useRef, useState } from "react";
import {
  ComboBox,
  FieldError,
  Input,
  type Key,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Text,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { problemText } from "../sign-in/messages";
import { markedName, VisibleText } from "./VisibleText";

/** How long typing must pause before the server is asked, in milliseconds. */
const PICKER_DELAY_MS = 250;

/** How many suggestions are asked for at once. */
export const PICKER_LIMIT = 20;

/** One suggestion. */
export interface NameChoice<T> {
  /** Tells it apart from the others, such as `gold.orders` for a table. */
  readonly key: string;
  /** The name the field takes when it is chosen: what the server is sent. */
  readonly name: string;
  /** A quiet word beside the name, such as "Service account" or the table's namespace. */
  readonly detail: string | null;
  /** What the caller needs when it is chosen, such as the table's namespace. */
  readonly data: T;
}

/** One page of suggestions. */
export interface NameMatches<T> {
  /** The suggestions, at most {@link PICKER_LIMIT}. */
  readonly choices: readonly NameChoice<T>[];
  /** Whether more match than were sent. */
  readonly more: boolean;
}

/**
 * Asks the server for the names that contain a text.
 *
 * @template T - What a suggestion carries.
 */
export type NameSearch<T> = (text: string, signal: AbortSignal) => Promise<NameMatches<T>>;

/** Properties of {@link NamePicker}. */
export interface NamePickerProps<T> {
  /** The label. */
  readonly label: string;
  /** What is typed. */
  readonly value: string;
  /** Called as the person types, pastes or chooses. */
  readonly onChange: (value: string) => void;
  /**
   * Asks for suggestions; null when the person may not list these names, which leaves a plain
   * field that asks the server nothing.
   */
  readonly search: NameSearch<T> | null;
  /** The sentence for a text nothing matches, such as `No principal named “ben”.`. */
  readonly noMatch: (text: string) => string;
  /** Called with a suggestion once it is chosen, after `onChange`. */
  readonly onPick?: (choice: NameChoice<T>) => void;
  /** A help line under the field. */
  readonly help?: string;
  /** A refusal of what is typed, under the field. */
  readonly error?: string | null;
  /** Whether the field must be filled. */
  readonly isRequired?: boolean;
  /** Whether the field cannot be used. */
  readonly isDisabled?: boolean;
  /** The input, for the focus. */
  readonly inputRef?: Ref<HTMLInputElement>;
  /** Called when the field is left. */
  readonly onBlur?: () => void;
  /** Called on Enter that chose no suggestion, after the field's own handling. */
  readonly onEnter?: () => void;
}

/** What the list shows for one text. */
type Found<T> =
  | { readonly state: "idle" }
  | { readonly state: "loading"; readonly text: string; readonly choices: readonly NameChoice<T>[] }
  | {
      readonly state: "done";
      readonly text: string;
      readonly choices: readonly NameChoice<T>[];
      readonly more: boolean;
    }
  | { readonly state: "failed"; readonly text: string; readonly reason: string };

/** What {@link useNameSearch} gives the field. */
interface NameSearchState<T> {
  /** What the list shows, and for which text. */
  readonly found: Found<T>;
  /** Asks for a text after a delay in milliseconds. */
  readonly ask: (text: string, delay: number) => void;
  /** Keeps one chosen suggestion as the answer for its name, and stops waiting for any other. */
  readonly settle: (choice: NameChoice<T>) => void;
}

/**
 * Asks for suggestions as the text changes: after a pause in the typing, the newest text only.
 * A newer text aborts the request of an older one, and an answer that is not the newest one's
 * is dropped, so suggestions never arrive out of order.
 *
 * @template T - What a suggestion carries.
 * @param search - Asks the server; null for none.
 * @returns What the list shows and the ways to change it.
 */
function useNameSearch<T>(search: NameSearch<T> | null): NameSearchState<T> {
  const { t } = useTranslation();
  const [found, setFound] = useState<Found<T>>({ state: "idle" });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const running = useRef<AbortController | null>(null);
  const round = useRef(0);
  const stop = useCallback((): void => {
    clearTimeout(timer.current);
    running.current?.abort();
    running.current = null;
    round.current += 1;
  }, []);
  // A new search (another environment's tables) stops waiting for the old one's answer...
  useEffect(() => stop, [search, stop]);
  // ...and shows none of its suggestions: they would name what the new one does not offer.
  // Kept in an object: React would call a function handed to a state setter.
  const [searchOf, setSearchOf] = useState({ search });
  if (searchOf.search !== search) {
    setSearchOf({ search });
    setFound({ state: "idle" });
  }
  const settle = useCallback(
    (choice: NameChoice<T>): void => {
      stop();
      setFound({ state: "done", text: choice.name, choices: [choice], more: false });
    },
    [stop],
  );
  const ask = useCallback(
    (text: string, delay: number): void => {
      stop();
      if (search === null) return;
      const mine = round.current;
      setFound((previous) => ({
        state: "loading",
        text,
        choices: previous.state === "done" ? previous.choices : [],
      }));
      timer.current = setTimeout(() => {
        const controller = new AbortController();
        running.current = controller;
        search(text.trim(), controller.signal).then(
          (matches) => {
            if (mine === round.current) {
              setFound({ state: "done", text, choices: matches.choices, more: matches.more });
            }
          },
          (error: unknown) => {
            if (mine === round.current && !controller.signal.aborted) {
              setFound({ state: "failed", text, reason: problemText(t, error) });
            }
          },
        );
      }, delay);
    },
    [search, stop, t],
  );
  return { found, ask, settle };
}

/**
 * What the list says besides its suggestions, and what a screen reader is told.
 *
 * @template T - What a suggestion carries.
 * @param t - The translator.
 * @param found - What the list shows.
 * @param noMatch - The sentence for a text nothing matches.
 * @returns The sentence, or null.
 */
function stateLine<T>(
  t: ReturnType<typeof useTranslation>["t"],
  found: Found<T>,
  noMatch: (text: string) => string,
): string | null {
  switch (found.state) {
    case "idle":
      return null;
    case "loading":
      return t("picker.searching");
    case "failed":
      return t("picker.failed", { reason: found.reason });
    case "done":
      if (found.choices.length === 0) {
        return found.text.trim() === "" ? t("picker.nothing") : noMatch(found.text.trim());
      }
      return found.more ? t("picker.more", { count: found.choices.length }) : null;
  }
}

/**
 * Renders the field and its list of suggestions (a React Aria `ComboBox`). Typing, pasting or
 * Arrow Down asks the server after a short pause; the list shows each name with its hidden
 * characters marked, a quiet word beside it, and says while it waits, when nothing matches,
 * when the first page is all it shows and when the suggestions could not be read. Choosing a
 * suggestion puts its exact name in the field. Whatever is typed may be sent too: the server
 * judges it. Escape closes an open list and keeps the text, without closing a dialog the field
 * is in. Enter takes the highlighted suggestion; with none highlighted it sends the form, as in
 * every other field.
 *
 * @template T - What a suggestion carries.
 * @param props - The label, the value, the search and the handlers.
 * @param props.label - The label.
 * @param props.value - What is typed.
 * @param props.onChange - Called as the person types, pastes or chooses.
 * @param props.search - Asks for suggestions; null for a plain field.
 * @param props.noMatch - The sentence for a text nothing matches.
 * @param props.onPick - Called with a chosen suggestion.
 * @param props.help - A help line.
 * @param props.error - A refusal of what is typed.
 * @param props.isRequired - Whether the field must be filled.
 * @param props.isDisabled - Whether the field cannot be used.
 * @param props.inputRef - The input.
 * @param props.onBlur - Called when the field is left.
 * @param props.onEnter - Called on Enter.
 * @returns The field.
 */
export function NamePicker<T>({
  label,
  value,
  onChange,
  search,
  noMatch,
  onPick,
  help,
  error = null,
  isRequired = false,
  isDisabled = false,
  inputRef,
  onBlur,
  onEnter,
}: NamePickerProps<T>): JSX.Element {
  const { t } = useTranslation();
  const { found: asked, ask, settle } = useNameSearch(search);
  // Suggestions belong to the text they were asked for: a value set from outside (the field
  // emptied after an add, a filter from the address) shows none and opens no list.
  const found: Found<T> =
    asked.state !== "idle" && asked.text === value ? asked : { state: "idle" };
  // The chosen suggestion, with the search it came from: another search (another environment's
  // tables, groups after people) chose nothing.
  const [selected, setSelected] = useState<{
    readonly key: Key;
    readonly search: NameSearch<T> | null;
  } | null>(null);
  const selectedKey = selected !== null && selected.search === search ? selected.key : null;
  const open = useRef(false);
  const openAtKey = useRef(false);
  const pickedByKey = useRef(false);
  const choices = found.state === "done" || found.state === "loading" ? found.choices : [];
  const line = stateLine(t, found, noMatch);
  const shown = markedName(value);
  const choose = (key: Key | null): void => {
    // React Aria also sends null when a list is left without a choice (a custom value).
    const choice = choices.find((one) => one.key === key);
    if (choice === undefined) return;
    pickedByKey.current = true;
    setSelected({ key: choice.key, search });
    settle(choice);
    onChange(choice.name);
    onPick?.(choice);
  };
  return (
    <div className="name-picker">
      <ComboBox
        className="field"
        items={choices}
        inputValue={value}
        // The choice stays selected while the list holds it, the answer to Arrow Down after it
        // too: a selection that left and came back would read as a new one, and close the list.
        value={choices.some((one) => one.key === selectedKey) ? selectedKey : null}
        allowsCustomValue
        allowsEmptyCollection={search !== null && found.state !== "idle"}
        isRequired={isRequired}
        isDisabled={isDisabled}
        isInvalid={error !== null}
        onInputChange={(text) => {
          setSelected(null);
          onChange(text);
          ask(text, PICKER_DELAY_MS);
        }}
        onOpenChange={(isOpen, trigger) => {
          open.current = isOpen;
          if (isOpen && trigger === "manual") ask(value, 0);
        }}
        onChange={choose}
        onBlur={() => {
          onBlur?.();
        }}
      >
        <Label>{label}</Label>
        {/* The field's own keys run first (React Aria's), then these. */}
        <Input
          ref={inputRef}
          autoComplete="off"
          spellCheck={false}
          onKeyDownCapture={() => {
            openAtKey.current = open.current;
            pickedByKey.current = false;
          }}
          onKeyDown={(event) => {
            // An open list takes Escape: the dialog the field is in stays open.
            if (event.key === "Escape" && openAtKey.current) event.stopPropagation();
            if (event.key !== "Enter" || pickedByKey.current) return;
            // An Enter that chose no suggestion does what it does in every other field: React
            // Aria closes the open list and keeps the form from sending, so the form is sent
            // here. One that chose a suggestion handed it over through `onPick` already.
            if (openAtKey.current) event.currentTarget.form?.requestSubmit();
            onEnter?.();
          }}
        />
        {(help !== undefined || shown !== value) && (
          <Text slot="description">
            {help}
            {help !== undefined && shown !== value ? " " : null}
            {shown !== value && (
              <span className="name-picker-marked">
                {t("picker.marked")} <VisibleText text={value} name />
              </span>
            )}
          </Text>
        )}
        <FieldError>{error}</FieldError>
        <Popover className="float name-picker-float" placement="bottom start">
          <ListBox<NameChoice<T>> className="menu" aria-label={t("picker.suggestions")}>
            {(choice) => (
              <ListBoxItem
                id={choice.key}
                className="menu-item"
                // The raw name goes into the field once chosen; the name a screen reader speaks
                // (VoiceOver takes it for React Aria's announcements) shows hidden characters,
                // with the quiet word that tells two equal names apart (a table's namespace).
                textValue={choice.name}
                aria-label={
                  choice.detail === null
                    ? markedName(choice.name)
                    : `${markedName(choice.name)}, ${markedName(choice.detail)}`
                }
              >
                <span className="name-picker-name">
                  <VisibleText text={choice.name} name />
                </span>
                {choice.detail !== null && (
                  <span className="name-picker-detail">
                    <VisibleText text={choice.detail} name />
                  </span>
                )}
              </ListBoxItem>
            )}
          </ListBox>
          {/* Inside the list's popover: while it is open, React Aria hides the rest of the page
              from screen readers, a live region outside it too. */}
          <p className="name-picker-state" role="status">
            {line}
          </p>
        </Popover>
      </ComboBox>
    </div>
  );
}
