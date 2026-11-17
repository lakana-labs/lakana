/**
 * The fields of one kind of connection: its address first, then its settings, in the order of
 * the reference.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import {
  Button,
  FieldError,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  RadioButton,
  RadioField,
  RadioGroup,
  Select,
  SelectValue,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ConnectionKind } from "../api";
import { CaCertificateField } from "./CaCertificateField";
import type { CaChoice } from "./formBody";
import { type FieldSpec, kindFields } from "./kinds";
import "./formStrings";

/** Properties of {@link KindFields}. */
export interface KindFieldsProps {
  /** The kind. */
  readonly kind: ConnectionKind;
  /** The address fields as typed. */
  readonly address: Readonly<Record<string, string>>;
  /** The settings as typed; for `http` its `auth` decides which settings show. */
  readonly settings: Readonly<Record<string, string>>;
  /** The CA certificate. */
  readonly ca: CaChoice;
  /** Called with an address field's key and its new text. */
  readonly onAddress: (key: string, value: string) => void;
  /** Called with a setting's key and its new value. */
  readonly onSetting: (key: string, value: string) => void;
  /** Called with the new CA certificate choice. */
  readonly onCa: (ca: CaChoice) => void;
}

/**
 * A field's label: its key as the server and the reference name it, marked when it may be left
 * empty.
 *
 * @param field - The field.
 * @param optional - The words that mark an optional field.
 * @returns The label.
 */
function fieldLabel(field: FieldSpec, optional: (key: string) => string): string {
  return field.required ? field.key : optional(field.key);
}

/** Properties of {@link ChoiceSelect}. */
interface ChoiceSelectProps {
  /** The field. */
  readonly field: FieldSpec;
  /** The chosen value; empty for the default. */
  readonly value: string;
  /** Called with the value chosen. */
  readonly onChange: (value: string) => void;
}

/**
 * A setting with named values (`tls`, `auth`) as a Select, each value in words; an empty value
 * shows the default, which the server uses when the setting is left out.
 *
 * @param props - The field, its value and where a choice goes.
 * @param props.field - The field.
 * @param props.value - The chosen value.
 * @param props.onChange - Called with the value chosen.
 * @returns The Select.
 */
function ChoiceSelect({ field, value, onChange }: ChoiceSelectProps): JSX.Element {
  const { t } = useTranslation();
  const nameId = useId();
  const chosen = value === "" ? String(field.default) : value;
  const words = (choice: string): string => t(`connections.${field.key}.${choice}`);
  return (
    <Select
      className="field connection-select"
      value={chosen}
      onChange={(key) => {
        onChange(String(key));
      }}
    >
      <Label>{field.key}</Label>
      <Button className="button" aria-labelledby={nameId}>
        <span id={nameId} className="visually-hidden">
          {t("connections.form.choice", { field: field.key, value: words(chosen) })}
        </span>
        <SelectValue aria-hidden="true" />
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom start">
        <ListBox className="menu">
          {(field.choices as readonly string[]).map((choice) => (
            <ListBoxItem key={choice} id={choice} className="menu-item" textValue={words(choice)}>
              <span className="menu-check" aria-hidden="true" />
              {words(choice)}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </Select>
  );
}

/** Properties of {@link KindField}. */
interface KindFieldProps {
  /** The field. */
  readonly field: FieldSpec;
  /** Whether it is an `s3` connection's endpoint, which defaults to the region's. */
  readonly regionDefault: boolean;
  /** Its text. */
  readonly value: string;
  /** Called with the new text. */
  readonly onChange: (value: string) => void;
}

/**
 * One field typed as text. A port left empty is the kind's default, shown as the placeholder
 * and said under the field; an `s3` endpoint left empty is the region's.
 *
 * @param props - The field, its text and where a change goes.
 * @param props.field - The field.
 * @param props.regionDefault - Whether it defaults to the region's endpoint.
 * @param props.value - The text.
 * @param props.onChange - Called with the new text.
 * @returns The field.
 */
function KindField({ field, regionDefault, value, onChange }: KindFieldProps): JSX.Element {
  const { t } = useTranslation();
  const port = field.key === "port" && field.default !== undefined;
  let description: string | null = null;
  if (port) {
    description = t("connections.form.portDefault", { port: String(field.default) });
  } else if (regionDefault) {
    description = t("connections.form.endpointDefault");
  }
  return (
    <TextField className="field" value={value} onChange={onChange} isRequired={field.required}>
      <Label>{fieldLabel(field, (key) => t("connections.form.optional", { field: key }))}</Label>
      <Input {...(port ? { placeholder: String(field.default), inputMode: "numeric" } : {})} />
      {description !== null && <Text slot="description">{description}</Text>}
      <FieldError />
    </TextField>
  );
}

/**
 * Renders the kind's fields, address first, then settings: text fields, `tls` and `auth` as
 * Selects, `api_key_in` as two radios, and the CA certificate as a file. For `http` the
 * settings follow `auth`. The server stays the judge of every value.
 *
 * @param props - The kind, what is typed, and where changes go.
 * @param props.kind - The kind.
 * @param props.address - The address fields.
 * @param props.settings - The settings.
 * @param props.ca - The CA certificate.
 * @param props.onAddress - Called with an address field's change.
 * @param props.onSetting - Called with a setting's change.
 * @param props.onCa - Called with a new CA certificate choice.
 * @returns The fields.
 */
export function KindFields({
  kind,
  address,
  settings,
  ca,
  onAddress,
  onSetting,
  onCa,
}: KindFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const fields = kindFields(kind, settings);
  const optional = (key: string): string => t("connections.form.optional", { field: key });
  return (
    <>
      {fields.address.map((field) => (
        <KindField
          key={`address ${field.key}`}
          field={field}
          regionDefault={kind === "s3" && field.key === "endpoint"}
          value={address[field.key] ?? ""}
          onChange={(value) => {
            onAddress(field.key, value);
          }}
        />
      ))}
      {fields.settings.map((field) => {
        const value = settings[field.key] ?? "";
        const change = (next: string): void => {
          onSetting(field.key, next);
        };
        if (field.pem === true) {
          return (
            <CaCertificateField
              key={field.key}
              label={fieldLabel(field, optional)}
              value={ca}
              onChange={onCa}
            />
          );
        }
        if (field.key === "api_key_in") {
          return (
            <RadioGroup
              key={field.key}
              className="field"
              orientation="horizontal"
              value={value === "" ? String(field.default) : value}
              onChange={change}
            >
              <Label>{field.key}</Label>
              {(field.choices as readonly string[]).map((choice) => (
                <RadioField className="radio-field" key={choice} value={choice}>
                  <RadioButton className="radio">
                    {t(`connections.form.apiKeyIn.${choice}`)}
                  </RadioButton>
                </RadioField>
              ))}
            </RadioGroup>
          );
        }
        if (field.choices !== undefined) {
          return <ChoiceSelect key={field.key} field={field} value={value} onChange={change} />;
        }
        return (
          <KindField
            key={`settings ${field.key}`}
            field={field}
            regionDefault={false}
            value={value}
            onChange={change}
          />
        );
      })}
    </>
  );
}
