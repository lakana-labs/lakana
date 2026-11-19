/**
 * The secret of a connection: new values typed into password fields, or an existing secret of
 * the environment by name.
 *
 * @remarks
 * A value is never React state: each value input is an uncontrolled `type="password"` input,
 * read through {@link SecretInputs} only when Save builds the request, since React copies a
 * controlled input's value into its `value` attribute, a password field's too. The `tcp` rows
 * hold only their id and name. Every value input has `autoComplete="new-password"`,
 * `data-1p-ignore` and `data-lpignore`, and nothing here logs, stores or renders a value.
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Button,
  FieldError,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ConnectionKind } from "../api";
import { markedText } from "../shell/VisibleText";
import { TextFileInput } from "./CaCertificateField";
import type { SecretChoice } from "./formBody";
import { FREE_KEYS_MAX, SECRET_VALUE_BYTES, secretKeys } from "./kinds";
import "./formStrings";

/** A `tcp` key's name, as the server takes it. */
const KEY_NAME = /^[a-z][a-z0-9_]{0,31}$/;

/** The value inputs the secret's fields have mounted, from which Save reads the values. */
export interface SecretInputs {
  /** Gives a value input its ref: `id` names the input, `key` the secret key it holds. */
  readonly field: (id: string, key: string) => (input: HTMLInputElement | null) => void;
  /** Puts text into the input of a key, as if typed (a key read from a file); none if gone. */
  readonly fill: (key: string, text: string) => void;
  /** The values of the mounted inputs, by key; inputs whose key is not named yet left out. */
  readonly read: () => Readonly<Record<string, string>>;
}

/**
 * Keeps the secret's value inputs, so the values are read from them only when Save sends.
 *
 * @returns The inputs' registry.
 */
export function useSecretInputs(): SecretInputs {
  const inputs = useRef(
    new Map<string, { readonly key: string; readonly input: HTMLInputElement }>(),
  );
  const field = useCallback(
    (id: string, key: string) => (input: HTMLInputElement | null) => {
      if (input === null) {
        inputs.current.delete(id);
      } else {
        inputs.current.set(id, { key, input });
      }
    },
    [],
  );
  const fill = useCallback((key: string, text: string) => {
    const input = inputs.current.get(key)?.input;
    if (input === undefined) {
      // The field went (another kind or way) while the file was read; so does the text.
      return;
    }
    input.value = text;
    // The field's check runs on input, as for a typed value.
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, []);
  const read = useCallback(
    () =>
      Object.fromEntries(
        [...inputs.current.values()]
          .filter(({ key }) => key !== "")
          .map(({ key, input }) => [key, input.value]),
      ),
    [],
  );
  return useMemo(() => ({ field, fill, read }), [field, fill, read]);
}

/** The secret a stored connection holds, as Edit shows it. */
export interface KeptSecret {
  /** Its name; null when the connection has none. */
  readonly name: string | null;
  /** Whether the change in the form could send it somewhere new. */
  readonly moves: boolean;
}

/** Properties of {@link SecretFields}. */
export interface SecretFieldsProps {
  /** The kind. */
  readonly kind: ConnectionKind;
  /** The settings; for `http` its `auth` decides the key. */
  readonly settings: Readonly<Record<string, string>>;
  /** The environment, named in the choice of an existing secret. */
  readonly environment: string;
  /** Whether the person may bind an existing secret there (`name_secret`). */
  readonly mayName: boolean;
  /** On Edit, the stored connection's secret; absent on Add. */
  readonly kept?: KeptSecret;
  /** The secret as the form holds it; new values stay in their inputs. */
  readonly value: SecretChoice;
  /** Called with the new secret. */
  readonly onChange: (value: SecretChoice) => void;
  /** The value inputs, read when Save sends. */
  readonly inputs: SecretInputs;
}

/** Properties of {@link PasswordField}. */
interface PasswordFieldProps {
  /** The label. */
  readonly label: string;
  /** Whether a value is needed. */
  readonly required: boolean;
  /** Gives the input its ref. */
  readonly inputRef: (input: HTMLInputElement | null) => void;
}

/**
 * One secret value: an uncontrolled password field (React Aria's `TextField` is always
 * controlled), marked as a new password so the browser fills no saved one in (browsers ignore
 * `off` on a password field), and out of the password managers' reach (`data-1p-ignore`,
 * `data-lpignore`). Like the form's other fields, a missing value shows once Save is tried: the
 * field is marked invalid with the browser's sentence (or ours when it gives none), and the first invalid field of the form
 * takes the focus; typing clears it until the next Save.
 *
 * @param props - The label, whether it is needed, and the ref.
 * @param props.label - The label.
 * @param props.required - Whether a value is needed.
 * @param props.inputRef - Gives the input its ref.
 * @returns The field.
 */
function PasswordField({ label, required, inputRef }: PasswordFieldProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const error = useId();
  const [problem, setProblem] = useState<string | null>(null);
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
        required={required}
        // No tooltip of the browser's own beside the sentence (Firefox), as React Aria does.
        title=""
        aria-invalid={problem === null ? undefined : true}
        aria-describedby={problem === null ? undefined : error}
        onInvalid={(event) => {
          const input = event.currentTarget;
          // No browser bubble; the sentence shows under the field, as React Aria's fields do.
          event.preventDefault();
          setProblem(input.validationMessage || t("connections.form.valueNeeded"));
          const form = input.form as HTMLFormElement;
          const first = [...form.elements].find(
            (element) => !(element as HTMLInputElement).validity.valid,
          );
          if (first === input) {
            input.focus();
          }
        }}
        onInput={() => {
          // The next Save checks it again.
          setProblem(null);
        }}
      />
      {problem !== null ? (
        <span id={error} slot="errorMessage">
          {problem}
        </span>
      ) : null}
    </div>
  );
}

/** One `tcp` key while it is typed; its value stays in the row's password field. */
interface Row {
  /** The row's own id, for React. */
  readonly id: number;
  /** The key's name. */
  readonly name: string;
}

/** Properties of {@link FreeKeys}. */
interface FreeKeysProps {
  /** The value inputs. */
  readonly inputs: SecretInputs;
}

/**
 * The keys of a `tcp` secret: 0 to 16 rows of a name and a value, with Add a key and Remove.
 * A name is checked when the field is left and on Save; a name given twice holds Save back.
 * Each row's fields and Remove carry its number, so no two controls share a name. A new row's
 * name takes the focus (Add a key goes once there are 16); after a Remove, Add a key takes it.
 *
 * @param props - The value inputs.
 * @param props.inputs - The value inputs, each under its row's name.
 * @returns The rows.
 */
function FreeKeys({ inputs }: FreeKeysProps): JSX.Element {
  const { t } = useTranslation();
  const [rows, setRows] = useState<readonly Row[]>([]);
  const next = useRef(0);
  const add = useRef<HTMLButtonElement>(null);
  const names = useRef(new Map<number, HTMLInputElement>());
  const [removed, setRemoved] = useState(0);
  const [added, setAdded] = useState<number | null>(null);
  useEffect(() => {
    if (added !== null) {
      names.current.get(added)?.focus();
    }
  }, [added]);
  useEffect(() => {
    if (removed > 0) {
      // The pressed Remove is gone with its row; Add a key holds the focus.
      add.current?.focus();
    }
  }, [removed]);
  const update = (id: number, name: string): void => {
    setRows(rows.map((row) => (row.id === id ? { ...row, name } : row)));
  };
  return (
    <div className="connection-keys">
      {rows.map((row, index) => {
        const number = index + 1;
        return (
          <div className="connection-key" key={row.id}>
            <TextField
              className="field"
              value={row.name}
              isRequired
              validate={(name) => {
                if (!KEY_NAME.test(name)) {
                  return t("connections.form.keyRule");
                }
                return rows.slice(0, index).some((earlier) => earlier.name === name)
                  ? t("connections.form.keyTwice")
                  : null;
              }}
              onChange={(name) => {
                update(row.id, name);
              }}
            >
              <Label>{t("connections.form.keyName", { number })}</Label>
              <Input
                ref={(input) => {
                  // A ref that returns its cleanup is never called with null (React 19).
                  names.current.set(row.id, input as HTMLInputElement);
                  return () => {
                    names.current.delete(row.id);
                  };
                }}
              />
              <FieldError />
            </TextField>
            <PasswordField
              label={t("connections.form.keyValue", { number })}
              required
              inputRef={inputs.field(`key-${String(row.id)}`, row.name)}
            />
            <Button
              className="button"
              onPress={() => {
                setRows(rows.filter(({ id }) => id !== row.id));
                setRemoved((count) => count + 1);
              }}
            >
              {t("connections.form.removeKey", { number })}
            </Button>
          </div>
        );
      })}
      {rows.length < FREE_KEYS_MAX ? (
        <Button
          ref={add}
          className="button"
          onPress={() => {
            next.current += 1;
            setRows([...rows, { id: next.current, name: "" }]);
            setAdded(next.current);
          }}
        >
          {t("connections.form.addKey")}
        </Button>
      ) : (
        <p>{t("connections.form.keysMax", { max: FREE_KEYS_MAX })}</p>
      )}
    </div>
  );
}

/** Properties of {@link NewValues}. */
interface NewValuesProps {
  /** The kind. */
  readonly kind: ConnectionKind;
  /** The settings. */
  readonly settings: Readonly<Record<string, string>>;
  /** The value inputs. */
  readonly inputs: SecretInputs;
}

/**
 * The fields of new values: one password field per key, the needed ones required; for `azure`
 * the choice of account key or SAS token, then that one; for `gcs` also a file the key is read
 * from; for `tcp` the person's own keys.
 *
 * @param props - The kind, its settings and the value inputs.
 * @param props.kind - The kind.
 * @param props.settings - The settings.
 * @param props.inputs - The value inputs, each under its key.
 * @returns The fields.
 */
function NewValues({ kind, settings, inputs }: NewValuesProps): JSX.Element {
  const { t } = useTranslation();
  const { required, optional, oneOf, free } = secretKeys(kind, settings);
  const [picked, setPicked] = useState<string | null>(null);
  if (free) {
    return <FreeKeys inputs={inputs} />;
  }
  const chosen = oneOf === null ? null : (picked ?? (oneOf[0] as string));
  const needed = chosen === null ? required : [chosen];
  return (
    <>
      {oneOf !== null && (
        <RadioGroup
          className="field"
          orientation="horizontal"
          value={chosen}
          onChange={(key) => {
            // Only the chosen key is ever sent; the other's field goes, and its value with it.
            setPicked(key);
          }}
        >
          <Label>{t("connections.form.azureKey")}</Label>
          {oneOf.map((key) => (
            <RadioField className="radio-field" key={key} value={key}>
              <RadioButton className="radio">{t(`connections.form.azure.${key}`)}</RadioButton>
            </RadioField>
          ))}
        </RadioGroup>
      )}
      {needed.map((key) => (
        <PasswordField key={key} label={key} required inputRef={inputs.field(key, key)} />
      ))}
      {optional.map((key) => (
        <PasswordField
          key={key}
          label={t("connections.form.optional", { field: key })}
          required={false}
          inputRef={inputs.field(key, key)}
        />
      ))}
      {needed.includes("service_account_json") && (
        <TextFileInput
          label={t("connections.form.keyFile")}
          accept=".json,application/json"
          max={SECRET_VALUE_BYTES}
          tooBig={t("connections.form.keyFileTooBig")}
          onText={(text) => {
            inputs.fill("service_account_json", text);
          }}
        />
      )}
    </>
  );
}

/**
 * A choice of the secret, started afresh: no value and no name typed.
 *
 * @param way - The choice.
 * @returns The secret.
 */
function freshChoice(way: string): SecretChoice {
  switch (way) {
    case "keep":
      return { way: "keep" };
    case "named":
      return { way: "named", name: "" };
    case "none":
      return { way: "none" };
    default:
      return { way: "values", values: {} };
  }
}

/** Properties of {@link KeepChoice}. */
interface KeepChoiceProps {
  /** The secret's name. */
  readonly name: string;
  /** Whether the change in the form could send it somewhere new. */
  readonly moves: boolean;
  /** Whether the person may manage secrets, and so may keep it while it moves. */
  readonly mayName: boolean;
}

/**
 * Keep the bound secret. When the change could send it somewhere new, the line under it says
 * where it goes, or, for a person who may not manage secrets, why it cannot be kept: the
 * server asks for its values again.
 *
 * @param props - The secret, whether it moves, and the right.
 * @param props.name - The secret's name.
 * @param props.moves - Whether it moves.
 * @param props.mayName - Whether the person may manage secrets.
 * @returns The radio.
 */
function KeepChoice({ name, moves, mayName }: KeepChoiceProps): JSX.Element {
  const { t } = useTranslation();
  const secret = markedText(name);
  return (
    <RadioField className="radio-field" value="keep" isDisabled={moves && !mayName}>
      <RadioButton className="radio">{t("connections.form.secretKeep", { secret })}</RadioButton>
      {moves && (
        <Text slot="description">
          {mayName
            ? t("connections.form.keepSent", { secret })
            : t("connections.form.keepMoves", { secret })}
        </Text>
      )}
    </RadioField>
  );
}

/**
 * Renders the secret. On Add, where the environment allows `name_secret`, the choice of new
 * values (the default) or an existing secret of the environment by name; else new values only.
 * On Edit, the choice of keeping the bound secret, new values, another secret (with
 * `name_secret`) or none.
 *
 * @param props - The kind, the environment, the right and the secret.
 * @param props.kind - The kind.
 * @param props.settings - The settings.
 * @param props.environment - The environment.
 * @param props.mayName - Whether the person may bind an existing secret.
 * @param props.kept - On Edit, the stored connection's secret.
 * @param props.value - The secret as the form holds it.
 * @param props.onChange - Called with the new secret.
 * @param props.inputs - The value inputs, read when Save sends.
 * @returns The fields.
 */
export function SecretFields({
  kind,
  settings,
  environment,
  mayName,
  kept,
  value,
  onChange,
  inputs,
}: SecretFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const legend = useId();
  const shown = markedText(environment);
  return (
    <fieldset className="secret-fields">
      <legend id={legend}>{t("connections.form.secret")}</legend>
      {(mayName || kept !== undefined) && (
        <RadioGroup
          className="field"
          aria-labelledby={legend}
          value={value.way}
          onChange={(way) => {
            onChange(freshChoice(way));
          }}
        >
          {kept !== undefined && kept.name !== null && (
            <KeepChoice name={kept.name} moves={kept.moves} mayName={mayName} />
          )}
          <RadioField className="radio-field" value="values">
            <RadioButton className="radio">{t("connections.form.secretValues")}</RadioButton>
          </RadioField>
          {mayName && (
            <RadioField className="radio-field" value="named">
              <RadioButton className="radio">
                {kept === undefined
                  ? t("connections.form.secretNamed", { environment: shown })
                  : t("connections.form.secretAnother", { environment: shown })}
              </RadioButton>
            </RadioField>
          )}
          {kept !== undefined && (
            <RadioField className="radio-field" value="none">
              <RadioButton className="radio">
                {kept.name === null
                  ? t("connections.form.secretNoneYet")
                  : t("connections.form.secretStop")}
              </RadioButton>
            </RadioField>
          )}
        </RadioGroup>
      )}
      {value.way === "named" && (
        <TextField
          className="field"
          isRequired
          value={value.name}
          onChange={(name) => {
            onChange({ way: "named", name });
          }}
        >
          <Label>{t("connections.form.secretName")}</Label>
          <Input />
          <FieldError />
        </TextField>
      )}
      {value.way === "values" && (
        <>
          <p className="connection-hint">{t("connections.form.secretHelp")}</p>
          <NewValues kind={kind} settings={settings} inputs={inputs} />
        </>
      )}
    </fieldset>
  );
}
