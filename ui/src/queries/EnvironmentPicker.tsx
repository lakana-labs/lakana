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

/**
 * The key of the option "All environments": a number, so no environment's name is ever it. A
 * picker without that option and without a value selects it, which selects nothing.
 */
const ALL = 0;

/** Properties of {@link EnvironmentPicker} that choose one environment. */
interface OnePickerProps {
  /** The environments where the person may run a query, as the server lists them. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The chosen environment's name; null for none. */
  readonly value: string | null;
  /** Called with the name of the environment chosen. */
  readonly onChange: (environment: string) => void;
  /** The label; "Environment" by default. */
  readonly label?: string;
  /** No option for all environments. */
  readonly all?: undefined;
}

/** Properties of {@link EnvironmentPicker} that choose one environment or all of them. */
interface FilterPickerProps {
  /** The environments where the person may run a query, as the server lists them. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The chosen environment's name; null for all of them. */
  readonly value: string | null;
  /** Called with the name of the environment chosen, or null for all of them. */
  readonly onChange: (environment: string | null) => void;
  /** The label. */
  readonly label: string;
  /** The words of the first option, all environments. */
  readonly all: string;
}

/** Properties of {@link EnvironmentPicker}: one environment, or one or all of them (a filter). */
export type EnvironmentPickerProps = OnePickerProps | FilterPickerProps;

/**
 * Renders the picker: the label "Environment", the chosen environment on a button whose name
 * says "Environment: prod, protected", and the environments in a list. As a filter, its first
 * option is all environments ("Show: All environments").
 *
 * @param props - The environments, the chosen one and what to do with a choice.
 * @param props.environments - The environments where the person may run a query.
 * @param props.value - The chosen environment's name.
 * @param props.onChange - Called with the name of the environment chosen.
 * @param props.label - The label.
 * @param props.all - The words of the option for all environments, for a filter.
 * @returns The picker.
 */
export function EnvironmentPicker({
  environments,
  value,
  onChange,
  label,
  all,
}: EnvironmentPickerProps): JSX.Element {
  const { t } = useTranslation();
  const nameId = useId();
  const chosen = environments.find((environment) => environment.name === value);
  const named = label ?? t("queries.environment.label");
  // A filter's onChange takes null too; the one-environment picker never offers it.
  const choose = onChange as (environment: string | null) => void;
  return (
    <Select
      className="env-picker"
      value={value ?? ALL}
      onChange={(key) => {
        // The keys are the environments' names, and ALL.
        choose(key === ALL ? null : String(key));
      }}
    >
      <Label>{named}</Label>
      <Button className="button" aria-labelledby={nameId}>
        <span id={nameId} className="visually-hidden">
          {t(
            chosen?.protected === true
              ? "queries.environment.triggerProtected"
              : "queries.environment.trigger",
            { label: named, environment: value ?? all },
          )}
        </span>
        <SelectValue aria-hidden="true">
          {chosen === undefined ? (
            value === null ? (
              all
            ) : null
          ) : (
            <EnvironmentName environment={chosen} />
          )}
        </SelectValue>
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom end">
        <ListBox className="menu">
          {all !== undefined && (
            <ListBoxItem id={ALL} className="menu-item" textValue={all}>
              <span className="menu-check" aria-hidden="true" />
              {all}
            </ListBoxItem>
          )}
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
