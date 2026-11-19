/**
 * A secret's values as a form writes them, and an environment's storage options likewise: rows
 * of a key and a value, the value typed into a password field and never shown.
 *
 * @remarks
 * A value is never React state: its password field is uncontrolled, so React never writes it
 * into the field's `value` attribute (it does so for a controlled input, a password field
 * included), and the dialog reads it from the field only when it sends. The rows hold only
 * their keys. Nothing here puts a value in the address, the storage, a log, an error, an
 * attribute or an accessible name; the field is gone, and its value with it, when the dialog
 * closes.
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useCallback, useId, useRef, useState } from "react";
import { Button, FieldError, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { markedText } from "../shell/VisibleText";
import "./secretStrings";

/** One row of the values: its key; the value stays in the row's password field. */
export interface ValueRow {
  /** The row's id, stable while the row lives. */
  readonly id: string;
  /** The key. */
  readonly key: string;
}

/** The rows' password fields, by row id, from which the values are read when the form sends. */
export interface ValueInputs {
  /** Gives a row's field its ref. */
  readonly register: (id: string) => (input: HTMLInputElement | null) => void;
  /** Reads the values as the server takes them, by key; the keys checked by {@link valuesReady}. */
  readonly read: (rows: readonly ValueRow[]) => Readonly<Record<string, string>>;
}

/**
 * Keeps the rows' password fields, so the values are read from them only when the form sends.
 *
 * @returns The fields' registry.
 */
export function useValueInputs(): ValueInputs {
  const inputs = useRef(new Map<string, HTMLInputElement>());
  const register = useCallback(
    (id: string) => (input: HTMLInputElement | null) => {
      if (input === null) {
        inputs.current.delete(id);
      } else {
        inputs.current.set(id, input);
      }
    },
    [],
  );
  const read = useCallback(
    (rows: readonly ValueRow[]) =>
      Object.fromEntries(
        // Every row's field is mounted while the form can send.
        rows.map((row) => [row.key.trim(), (inputs.current.get(row.id) as HTMLInputElement).value]),
      ),
    [],
  );
  return { register, read };
}

/** The strings of the secret's values, the default words of the fields. */
const SECRET_WORDS = "secrets.values";

/** Counts the rows made, for their ids. */
let made = 0;

/**
 * A new row.
 *
 * @param key - Its key; empty by default.
 * @returns The row.
 */
export function newRow(key = ""): ValueRow {
  made += 1;
  return { id: `value-${String(made)}`, key };
}

/**
 * What is wrong with a row's key, or null when nothing is: it must be named, and no other row
 * may have it.
 *
 * @param t - The translator.
 * @param rows - Every row.
 * @param row - The row.
 * @param words - The strings' prefix.
 * @returns The sentence, which names the key, never the value.
 */
function keyProblem(
  t: TFunction,
  rows: readonly ValueRow[],
  row: ValueRow,
  words: string,
): string | null {
  const key = row.key.trim();
  if (key === "") return t(`${words}.keyMissing`);
  const twice = rows.some((other) => other.id !== row.id && other.key.trim() === key);
  return twice ? t(`${words}.keyTwice`, { key }) : null;
}

/**
 * Whether every key is named and none is repeated, so the values may be sent.
 *
 * @param t - The translator.
 * @param rows - Every row.
 * @returns True when they may.
 */
export function valuesReady(t: TFunction, rows: readonly ValueRow[]): boolean {
  return rows.every((row) => keyProblem(t, rows, row, SECRET_WORDS) === null);
}

/**
 * Fills keys a use names into the rows: each key not there yet goes into the first row with
 * no key, else into a new row.
 *
 * @param rows - Every row.
 * @param keys - The keys the use names.
 * @returns The rows with the keys.
 */
export function withKeys(rows: readonly ValueRow[], keys: readonly string[]): readonly ValueRow[] {
  let next = [...rows];
  for (const key of keys) {
    if (next.some((row) => row.key.trim() === key)) continue;
    const empty = next.findIndex((row) => row.key.trim() === "");
    next =
      empty === -1
        ? [...next, newRow(key)]
        : next.map((row, index) => (index === empty ? { ...row, key } : row));
  }
  return next;
}

/** Properties of {@link SecretValuesFields}. */
export interface SecretValuesFieldsProps {
  /** The rows. */
  readonly rows: readonly ValueRow[];
  /** Called with the rows as changed. */
  readonly onChange: (rows: readonly ValueRow[]) => void;
  /** Whether a send was tried, so every key's problem shows, not only those left. */
  readonly checked: boolean;
  /** The rows' password fields. */
  readonly inputs: ValueInputs;
  /**
   * The prefix of the strings the fields say (its `legend`, `help`, `key`, `value`, `add`,
   * `remove`, `removeOf`, `removeUnnamed`, `keyMissing` and `keyTwice`); a secret's by default.
   */
  readonly words?: string;
  /** Whether the last row may be removed too, for values that may be none; no by default. */
  readonly mayBeNone?: boolean;
}

/** Properties of {@link ValueField}. */
interface ValueFieldProps {
  /** Gives the field its ref. */
  readonly inputRef: (input: HTMLInputElement | null) => void;
  /** Its label. */
  readonly label: string;
}

/**
 * One value: an uncontrolled password field, marked as a new password so the browser fills no
 * saved password in (browsers ignore `off` on a password field, as the Connections form's
 * fields say too), and with `data-1p-ignore` and `data-lpignore`, which ask password managers
 * to ignore it. A browser may still offer to save a typed value once the form is sent; the
 * person can decline.
 *
 * @param props - The ref and the label.
 * @param props.inputRef - Gives the field its ref.
 * @param props.label - Its label.
 * @returns The field.
 */
function ValueField({ inputRef, label }: ValueFieldProps): JSX.Element {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={inputRef}
        type="password"
        autoComplete="new-password"
        spellCheck={false}
        data-1p-ignore=""
        data-lpignore="true"
      />
    </div>
  );
}

/**
 * Renders the rows, each a Key field, a Value field (a password field, with no way to reveal
 * it) and Remove while there is more than one row (or any, with `mayBeNone`), then Add a value.
 * A key's problem shows once its field is left, or once a send was tried.
 *
 * @param props - The rows, the change handler, whether a send was tried and the fields.
 * @param props.rows - The rows.
 * @param props.onChange - Called with the rows as changed.
 * @param props.checked - Whether a send was tried.
 * @param props.inputs - The rows' password fields.
 * @param props.words - The prefix of the strings the fields say.
 * @param props.mayBeNone - Whether the last row may be removed too.
 * @returns The fields.
 */
export function SecretValuesFields({
  rows,
  onChange,
  checked,
  inputs,
  words = SECRET_WORDS,
  mayBeNone = false,
}: SecretValuesFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const [left, setLeft] = useState<ReadonlySet<string>>(new Set());
  const change = (id: string, part: Partial<ValueRow>): void => {
    onChange(rows.map((row) => (row.id === id ? { ...row, ...part } : row)));
  };
  return (
    <fieldset className="secret-values">
      <legend>{t(`${words}.legend`)}</legend>
      <p className="step-hint">{t(`${words}.help`)}</p>
      {rows.map((row) => {
        const problem = checked || left.has(row.id) ? keyProblem(t, rows, row, words) : null;
        return (
          <div className="secret-value-row" key={row.id}>
            <TextField
              className="field"
              value={row.key}
              isInvalid={problem !== null}
              onChange={(key) => {
                change(row.id, { key });
              }}
              onBlur={() => {
                setLeft((previous) => new Set(previous).add(row.id));
              }}
            >
              <Label>{t(`${words}.key`)}</Label>
              <Input autoComplete="off" spellCheck={false} />
              <FieldError>{problem}</FieldError>
            </TextField>
            <ValueField inputRef={inputs.register(row.id)} label={t(`${words}.value`)} />
            {(mayBeNone || rows.length > 1) && (
              <Button
                className="button"
                aria-label={
                  row.key.trim() === ""
                    ? t(`${words}.removeUnnamed`)
                    : t(`${words}.removeOf`, { key: markedText(row.key.trim()) })
                }
                onPress={() => {
                  onChange(rows.filter((other) => other.id !== row.id));
                }}
              >
                {t(`${words}.remove`)}
              </Button>
            )}
          </div>
        );
      })}
      <Button
        className="button"
        onPress={() => {
          onChange([...rows, newRow()]);
        }}
      >
        {t(`${words}.add`)}
      </Button>
    </fieldset>
  );
}
