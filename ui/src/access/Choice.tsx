/**
 * One labelled choice from a list, with "Choose…" until something is chosen: the forms of Add
 * grant and Add filter or mask share it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
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

import "./accessStrings";

/** Properties of {@link Choice}. */
export interface ChoiceProps<T extends string> {
  /** The label. */
  readonly label: string;
  /** The chosen key, or null. */
  readonly value: T | null;
  /** The options. */
  readonly options: readonly { readonly id: T; readonly text: string; readonly mark?: boolean }[];
  /** Called with the choice. */
  readonly onChange: (id: T) => void;
  /** Renders an option's content; its text by default. */
  readonly render?: (id: T) => JSX.Element;
}

/**
 * One labelled choice from a list, with "Choose…" until something is chosen.
 *
 * @template T - The keys.
 * @param props - The label, the choice, the options and the handler.
 * @param props.label - The label.
 * @param props.value - The chosen key.
 * @param props.options - The options.
 * @param props.onChange - Called with the choice.
 * @param props.render - Renders an option's content.
 * @returns The choice.
 */
export function Choice<T extends string>({
  label,
  value,
  options,
  onChange,
  render,
}: ChoiceProps<T>): JSX.Element {
  const { t } = useTranslation();
  return (
    <Select
      className="env-picker access-choice"
      value={value}
      placeholder={t("access.addGrant.choose")}
      onChange={(key) => {
        // The only keys are the options'.
        onChange(String(key) as T);
      }}
    >
      <Label>{label}</Label>
      <Button className="button">
        <SelectValue />
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom start">
        <ListBox className="menu">
          {options.map((option) => (
            <ListBoxItem
              key={option.id}
              id={option.id}
              className="menu-item"
              textValue={option.text}
            >
              <span className="menu-check" aria-hidden="true" />
              {render === undefined ? option.text : render(option.id)}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </Select>
  );
}
