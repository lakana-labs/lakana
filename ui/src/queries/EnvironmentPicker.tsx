/**
 * The environment a query runs in, chosen from those where the person may run one; a protected
 * environment is marked in the colour of danger and in words.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import {
  Button,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Select,
  SelectValue,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { QueryEnvironmentOut } from "../api/queries";
import { VisibleText } from "../shell/VisibleText";
import "./queryStrings";

/** Properties of {@link EnvironmentName}. */
interface EnvironmentNameProps {
  /** The environment. */
  readonly environment: QueryEnvironmentOut;
}

/**
 * An environment's name, and for a protected one the mark: a dot before it and the word
 * "protected" after it, both in the colour of danger, so the colour never stands alone
 * ("● prod · protected").
 *
 * @param props - The environment.
 * @param props.environment - The environment.
 * @returns The name.
 */
function EnvironmentName({ environment }: EnvironmentNameProps): JSX.Element {
  const { t } = useTranslation();
  if (!environment.protected) {
    return <VisibleText text={environment.name} />;
  }
  return (
    <>
      <span className="env-protected" aria-hidden="true">
        ●
      </span>
      <VisibleText text={environment.name} />
      <span className="env-protected">
        <span aria-hidden="true">· </span>
        {t("queries.environment.protected")}
      </span>
    </>
  );
}

/** Properties of {@link EnvironmentPicker}. */
export interface EnvironmentPickerProps {
  /** The environments where the person may run a query, as the server lists them. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The chosen environment's name; null for none. */
  readonly value: string | null;
  /** Called with the name of the environment chosen. */
  readonly onChange: (environment: string) => void;
}

/**
 * Renders the picker: the label "Environment", the chosen environment on a button whose name
 * says "Environment: prod, protected", and the environments in a list.
 *
 * @param props - The environments, the chosen one and what to do with a choice.
 * @param props.environments - The environments where the person may run a query.
 * @param props.value - The chosen environment's name.
 * @param props.onChange - Called with the name of the environment chosen.
 * @returns The picker.
 */
export function EnvironmentPicker({
  environments,
  value,
  onChange,
}: EnvironmentPickerProps): JSX.Element {
  const { t } = useTranslation();
  const nameId = useId();
  const chosen = environments.find((environment) => environment.name === value);
  return (
    <Select
      className="env-picker"
      value={value}
      onChange={(key) => {
        // The only keys are the environments' names.
        onChange(String(key));
      }}
    >
      <Label>{t("queries.environment.label")}</Label>
      <Button className="button" aria-labelledby={nameId}>
        <span id={nameId} className="visually-hidden">
          {t(
            chosen?.protected === true
              ? "queries.environment.triggerProtected"
              : "queries.environment.trigger",
            { environment: value },
          )}
        </span>
        <SelectValue aria-hidden="true">
          {chosen === undefined ? null : <EnvironmentName environment={chosen} />}
        </SelectValue>
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom end">
        <ListBox className="menu">
          {environments.map((environment) => (
            <ListBoxItem
              key={environment.name}
              id={environment.name}
              className="menu-item"
              textValue={environment.name}
            >
              <span className="menu-check" aria-hidden="true" />
              <EnvironmentName environment={environment} />
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </Select>
  );
}
