/**
 * The secret of a connection: new values typed into password fields, or an existing secret of
 * the environment by name.
 *
 * @remarks
 * The values live only in the form's state (and, for `tcp`, in the rows' state below it) until
 * Save sends them; every value input is `type="password"` with autocomplete off, and nothing
 * here logs, stores or renders a value.
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef, useState } from "react";
import {
  Button,
  FieldError,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ConnectionKind } from "../api";
import { TextFileInput } from "./CaCertificateField";
import { type SecretChoice, typedValues } from "./formBody";
import { FREE_KEYS_MAX, SECRET_VALUE_BYTES, secretKeys } from "./kinds";
import "./formStrings";

/** A `tcp` key's name, as the server takes it. */
const KEY_NAME = /^[a-z][a-z0-9_]{0,31}$/;

/** The values typed, by key. */
type Values = Readonly<Record<string, string>>;

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
  /** The secret as the form holds it. */
  readonly value: SecretChoice;
  /** Called with the new secret. */
  readonly onChange: (value: SecretChoice) => void;
}

/** Properties of {@link PasswordField}. */
interface PasswordFieldProps {
  /** The label. */
  readonly label: string;
  /** Whether a value is needed. */
  readonly required: boolean;
  /** The value. */
  readonly value: string;
  /** Called with the new value. */
  readonly onChange: (value: string) => void;
}

/**
 * One secret value: a password field with autocomplete off, so the browser neither shows nor
 * keeps it.
 *
 * @param props - The label, whether it is needed, and the value.
 * @param props.label - The label.
 * @param props.required - Whether a value is needed.
 * @param props.value - The value.
 * @param props.onChange - Called with the new value.
 * @returns The field.
 */
function PasswordField({ label, required, value, onChange }: PasswordFieldProps): JSX.Element {
  return (
    <TextField
      className="field"
      type="password"
      autoComplete="off"
      isRequired={required}
      value={value}
      onChange={onChange}
    >
      <Label>{label}</Label>
      <Input />
      <FieldError />
    </TextField>
  );
}

/** One `tcp` key while it is typed. */
interface Row {
  /** The row's own id, for React. */
  readonly id: number;
  /** The key's name. */
  readonly name: string;
  /** The key's value. */
  readonly value: string;
}

/** Properties of {@link FreeKeys}. */
interface FreeKeysProps {
  /** Called with the values by name, each time a row changes. */
  readonly onChange: (values: Values) => void;
}

/**
 * The keys of a `tcp` secret: 0 to 16 rows of a name and a value, with Add a key and Remove.
 * A name is checked when the field is left and on Save; a name given twice holds Save back.
 *
 * @param props - Where the values go.
 * @param props.onChange - Called with the values by name.
 * @returns The rows.
 */
function FreeKeys({ onChange }: FreeKeysProps): JSX.Element {
  const { t } = useTranslation();
  const [rows, setRows] = useState<readonly Row[]>([]);
  const next = useRef(0);
  const add = useRef<HTMLButtonElement>(null);
  const [removed, setRemoved] = useState(0);
  useEffect(() => {
    if (removed > 0) {
      // The pressed Remove is gone with its row; Add a key holds the focus.
      add.current?.focus();
    }
  }, [removed]);
  const change = (changed: readonly Row[]): void => {
    setRows(changed);
    onChange(
      Object.fromEntries(
        changed.filter(({ name }) => name !== "").map(({ name, value }) => [name, value]),
      ),
    );
  };
  const update = (id: number, part: Partial<Row>): void => {
    change(rows.map((row) => (row.id === id ? { ...row, ...part } : row)));
  };
  return (
    <div className="connection-keys">
      {rows.map((row, index) => (
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
              update(row.id, { name });
            }}
          >
            <Label>{t("connections.form.keyName")}</Label>
            <Input />
            <FieldError />
          </TextField>
          <PasswordField
            label={t("connections.form.keyValue")}
            required
            value={row.value}
            onChange={(value) => {
              update(row.id, { value });
            }}
          />
          <Button
            className="button"
            onPress={() => {
              change(rows.filter(({ id }) => id !== row.id));
              setRemoved((count) => count + 1);
            }}
          >
            {t("connections.form.removeKey")}
          </Button>
        </div>
      ))}
      {rows.length < FREE_KEYS_MAX ? (
        <Button
          ref={add}
          className="button"
          onPress={() => {
            next.current += 1;
            change([...rows, { id: next.current, name: "", value: "" }]);
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
  /** The values typed, by key. */
  readonly values: Values;
  /** Called with the values. */
  readonly onChange: (values: Values) => void;
}

/**
 * The fields of new values: one password field per key, the needed ones required; for `azure`
 * the choice of account key or SAS token, then that one; for `gcs` also a file the key is read
 * from; for `tcp` the person's own keys.
 *
 * @param props - The kind, its settings and the values.
 * @param props.kind - The kind.
 * @param props.settings - The settings.
 * @param props.values - The values typed.
 * @param props.onChange - Called with the values.
 * @returns The fields.
 */
function NewValues({ kind, settings, values, onChange }: NewValuesProps): JSX.Element {
  const { t } = useTranslation();
  const { required, optional, oneOf, free } = secretKeys(kind, settings);
  const [picked, setPicked] = useState<string | null>(null);
  if (free) {
    return <FreeKeys onChange={onChange} />;
  }
  const set = (key: string) => (value: string) => {
    onChange({ ...values, [key]: value });
  };
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
            setPicked(key);
            // Only the chosen key is ever sent; the other's value is forgotten.
            onChange({});
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
        <PasswordField
          key={key}
          label={key}
          required
          value={values[key] ?? ""}
          onChange={set(key)}
        />
      ))}
      {optional.map((key) => (
        <PasswordField
          key={key}
          label={t("connections.form.optional", { field: key })}
          required={false}
          value={values[key] ?? ""}
          onChange={set(key)}
        />
      ))}
      {needed.includes("service_account_json") && (
        <TextFileInput
          label={t("connections.form.keyFile")}
          accept=".json,application/json"
          max={SECRET_VALUE_BYTES}
          tooBig={t("connections.form.keyFileTooBig")}
          onText={set("service_account_json")}
        />
      )}
    </>
  );
}

/**
 * Renders the secret: where the environment allows `name_secret`, the choice of new values (the
 * default) or an existing secret of the environment by name; else new values only.
 *
 * @param props - The kind, the environment, the right and the secret.
 * @param props.kind - The kind.
 * @param props.settings - The settings.
 * @param props.environment - The environment.
 * @param props.mayName - Whether the person may bind an existing secret.
 * @param props.value - The secret as the form holds it.
 * @param props.onChange - Called with the new secret.
 * @returns The fields.
 */
export function SecretFields({
  kind,
  settings,
  environment,
  mayName,
  value,
  onChange,
}: SecretFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const legend = useId();
  const values = (next: Values): void => {
    onChange({ way: "values", values: next });
  };
  return (
    <fieldset className="secret-fields">
      <legend id={legend}>{t("connections.form.secret")}</legend>
      {mayName && (
        <RadioGroup
          className="field"
          aria-labelledby={legend}
          value={value.way}
          onChange={(way) => {
            onChange(way === "named" ? { way: "named", name: "" } : { way: "values", values: {} });
          }}
        >
          <RadioField className="radio-field" value="values">
            <RadioButton className="radio">{t("connections.form.secretValues")}</RadioButton>
          </RadioField>
          <RadioField className="radio-field" value="named">
            <RadioButton className="radio">
              {t("connections.form.secretNamed", { environment })}
            </RadioButton>
          </RadioField>
        </RadioGroup>
      )}
      {value.way === "named" ? (
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
      ) : (
        <>
          <p className="connection-hint">{t("connections.form.secretHelp")}</p>
          <NewValues
            kind={kind}
            settings={settings}
            values={typedValues(value)}
            onChange={values}
          />
        </>
      )}
    </fieldset>
  );
}
