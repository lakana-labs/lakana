/**
 * A secret's values as a form writes them: rows of a key and a value, the value typed into a
 * password field and never shown.
 *
 * @remarks
 * The values live only in the state of the dialog that holds these rows; nothing here puts one
 * in the address, the storage, a log, an error or an accessible name.
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useState } from "react";
import { Button, FieldError, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { markedText } from "../shell/VisibleText";
import "./secretStrings";

/** One row of the values: a key and its value. */
export interface ValueRow {
  /** The row's id, stable while the row lives. */
  readonly id: string;
  /** The key. */
  readonly key: string;
  /** The value, as typed. */
  readonly value: string;
}

/** Counts the rows made, for their ids. */
let made = 0;

/**
 * A new row.
 *
 * @param key - Its key; empty by default.
 * @returns The row, with an empty value.
 */
export function newRow(key = ""): ValueRow {
  made += 1;
  return { id: `value-${String(made)}`, key, value: "" };
}

/**
 * What is wrong with a row's key, or null when nothing is: it must be named, and no other row
 * may have it.
 *
 * @param t - The translator.
 * @param rows - Every row.
 * @param row - The row.
 * @returns The sentence, which names the key, never the value.
 */
function keyProblem(t: TFunction, rows: readonly ValueRow[], row: ValueRow): string | null {
  const key = row.key.trim();
  if (key === "") return t("secrets.values.keyMissing");
  const twice = rows.some((other) => other.id !== row.id && other.key.trim() === key);
  return twice ? t("secrets.values.keyTwice", { key }) : null;
}

/**
 * Whether every key is named and none is repeated, so the values may be sent.
 *
 * @param t - The translator.
 * @param rows - Every row.
 * @returns True when they may.
 */
export function valuesReady(t: TFunction, rows: readonly ValueRow[]): boolean {
  return rows.every((row) => keyProblem(t, rows, row) === null);
}

/**
 * The values as the server takes them.
 *
 * @param rows - Every row; their keys checked by {@link valuesReady}.
 * @returns The values by key.
 */
export function valuesOf(rows: readonly ValueRow[]): Readonly<Record<string, string>> {
  return Object.fromEntries(rows.map((row) => [row.key.trim(), row.value]));
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
}

/**
 * Renders the rows, each a Key field, a Value field (a password field, with no way to reveal
 * it) and Remove while there is more than one row, then Add a value. A key's problem shows once
 * its field is left, or once a send was tried.
 *
 * @param props - The rows, the change handler and whether a send was tried.
 * @param props.rows - The rows.
 * @param props.onChange - Called with the rows as changed.
 * @param props.checked - Whether a send was tried.
 * @returns The fields.
 */
export function SecretValuesFields({
  rows,
  onChange,
  checked,
}: SecretValuesFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const [left, setLeft] = useState<ReadonlySet<string>>(new Set());
  const change = (id: string, part: Partial<ValueRow>): void => {
    onChange(rows.map((row) => (row.id === id ? { ...row, ...part } : row)));
  };
  return (
    <fieldset className="secret-values">
      <legend>{t("secrets.values.legend")}</legend>
      <p className="step-hint">{t("secrets.values.help")}</p>
      {rows.map((row) => {
        const problem = checked || left.has(row.id) ? keyProblem(t, rows, row) : null;
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
              <Label>{t("secrets.values.key")}</Label>
              <Input autoComplete="off" spellCheck={false} />
              <FieldError>{problem}</FieldError>
            </TextField>
            <TextField
              className="field"
              type="password"
              value={row.value}
              onChange={(value) => {
                change(row.id, { value });
              }}
            >
              <Label>{t("secrets.values.value")}</Label>
              <Input autoComplete="off" spellCheck={false} />
            </TextField>
            {rows.length > 1 && (
              <Button
                className="button"
                aria-label={
                  row.key.trim() === ""
                    ? t("secrets.values.removeUnnamed")
                    : t("secrets.values.removeOf", { key: markedText(row.key.trim()) })
                }
                onPress={() => {
                  onChange(rows.filter((other) => other.id !== row.id));
                }}
              >
                {t("secrets.values.remove")}
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
        {t("secrets.values.add")}
      </Button>
    </fieldset>
  );
}
