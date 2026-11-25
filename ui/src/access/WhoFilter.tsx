/**
 * The Who filter the Grants list and Row filters & masks share: anyone, a person or a group,
 * then the name; and the text field both apply on Enter or when it is left.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { FieldError, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { GrantWho } from "../api/grants";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { NamePicker } from "../shell/NamePicker";
import "./accessStrings";
import { useWhoSearch } from "./suggest";

/** How a filter change is kept in the history: a menu choice is a step, a typed name is not. */
export type FilterStep = "push" | "replace";

/** Properties of {@link AppliedField}. */
interface AppliedFieldProps {
  /** The label. */
  readonly label: string;
  /** What is typed. */
  readonly value: string;
  /** Called as the person types. */
  readonly onChange: (value: string) => void;
  /** Applies what is typed: on Enter, or when the field is left. */
  readonly onApply: () => void;
  /** A help line under the field. */
  readonly help?: string;
  /** Whether the field cannot be used yet. */
  readonly isDisabled?: boolean;
  /** A refusal of what is typed, under the field. */
  readonly error?: string | null;
}

/**
 * A text field whose value is applied on Enter or when the field is left, never as one types.
 *
 * @param props - The label, the value and the handlers.
 * @param props.label - The label.
 * @param props.value - What is typed.
 * @param props.onChange - Called as the person types.
 * @param props.onApply - Applies the value.
 * @param props.help - A help line.
 * @param props.isDisabled - Whether the field cannot be used yet.
 * @param props.error - A refusal of what is typed.
 * @returns The field.
 */
export function AppliedField({
  label,
  value,
  onChange,
  onApply,
  help,
  isDisabled = false,
  error = null,
}: AppliedFieldProps): JSX.Element {
  return (
    <TextField
      className="field"
      value={value}
      onChange={onChange}
      onBlur={onApply}
      isDisabled={isDisabled}
      isInvalid={error !== null}
      onKeyDown={(event) => {
        if (event.key === "Enter") onApply();
      }}
    >
      <Label>{label}</Label>
      <Input autoComplete="off" spellCheck={false} />
      {help !== undefined && <Text slot="description">{help}</Text>}
      <FieldError>{error}</FieldError>
    </TextField>
  );
}

/** Who a list is filtered by. */
type WhoKind = "anyone" | GrantWho["kind"];

/** Properties of {@link WhoFilter}. */
export interface WhoFilterProps {
  /** Who the list shows, or null for anyone. */
  readonly who: GrantWho | null;
  /** Called with the next who, and how to keep it in the history. */
  readonly onChange: (who: GrantWho | null, step: FilterStep) => void;
}

/**
 * Renders the Who menu and, for a person or a group, the name, which suggests the principals or
 * the groups to an administrator. A menu choice and a chosen suggestion apply at once; a typed
 * name on Enter or when the field is left. A person or group without a name keeps the list as it
 * is.
 *
 * @param props - Who and the handler.
 * @param props.who - Who the list shows.
 * @param props.onChange - Called with the next who.
 * @returns The filter.
 */
export function WhoFilter({ who, onChange }: WhoFilterProps): JSX.Element {
  const { t } = useTranslation();
  const [kind, setKind] = useState<WhoKind>(who?.kind ?? "anyone");
  // An administrator is offered the names; anyone else may not list them, and types one.
  const suggest = useWhoSearch(kind === "group" ? "group" : "principal");
  const [name, setName] = useState(who?.name ?? "");
  // The who last read from the list, and the one these fields last sent: the address changes
  // after the send, so until then the list still shows the old who, which must not set the
  // fields back (the Name would go with "anyone", and the focus with it).
  const [shownFor, setShownFor] = useState(who);
  const [sent, setSent] = useState<GrantWho | null | undefined>(undefined);
  // A filter from elsewhere (Clear filters, Back) sets the fields again.
  if (JSON.stringify(shownFor) !== JSON.stringify(who)) {
    setShownFor(who);
    setSent(undefined);
    if (JSON.stringify(sent) !== JSON.stringify(who)) {
      setKind(who?.kind ?? "anyone");
      setName(who?.name ?? "");
    }
  }
  const apply = (chosen: WhoKind, typed: string, step: FilterStep): void => {
    const trimmed = typed.trim();
    let next: GrantWho | null | undefined;
    if (chosen === "anyone") next = null;
    else if (trimmed !== "") next = { kind: chosen, name: trimmed };
    if (next === undefined || JSON.stringify(next) === JSON.stringify(who)) return;
    // The fields already show it: the who that comes back must not set them again.
    setSent(next);
    onChange(next, step);
  };
  return (
    <>
      <ChoiceMenu<WhoKind>
        className="env-picker"
        label={t("access.grants.filters.who")}
        selected={kind}
        choices={[
          { id: "anyone", text: t("access.grants.filters.anyone") },
          { id: "principal", text: t("access.grants.filters.aPerson") },
          { id: "group", text: t("access.grants.filters.aGroup") },
        ]}
        onChoose={(chosen) => {
          setKind(chosen);
          apply(chosen, name, "push");
        }}
      />
      {kind !== "anyone" && (
        <NamePicker
          label={t("access.grants.filters.name")}
          value={name}
          onChange={setName}
          search={suggest.search}
          noMatch={suggest.noMatch}
          help={t("access.grants.filters.nameHelp")}
          onPick={(choice) => {
            apply(kind, choice.name, "replace");
          }}
          onEnter={() => {
            apply(kind, name, "replace");
          }}
          onBlur={() => {
            apply(kind, name, "replace");
          }}
        />
      )}
    </>
  );
}
