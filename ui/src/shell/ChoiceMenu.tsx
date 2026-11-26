/**
 * A labelled menu of a few choices, such as the Catalog's "Sort by" and the Jobs runs' "Status".
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

/** One choice of a {@link ChoiceMenu}. */
interface Choice<T extends string> {
  /** What the choice sets. */
  readonly id: T;
  /** The words it is written as. */
  readonly text: string;
}

/** Properties of {@link ChoiceMenu}. */
export interface ChoiceMenuProps<T extends string> {
  /** The menu's name. */
  readonly label: string;
  /** The choices, in order. */
  readonly choices: readonly Choice<T>[];
  /** The id of the chosen one. */
  readonly selected: T;
  /** Called with the id of a choice. */
  readonly onChoose: (id: T) => void;
  /** The class that lays out the label and the button; the Catalog's `sort-select` by default. */
  readonly className?: string;
  /** A short line under the choices that says where they come from; it describes the list. */
  readonly note?: string;
}

/**
 * One menu: a label, the chosen word on a button and the choices in a popover.
 *
 * @template T - The ids of the choices.
 * @param props - The label, the choices and the chosen one.
 * @param props.label - The menu's name.
 * @param props.choices - The choices, in order.
 * @param props.selected - The id of the chosen one.
 * @param props.onChoose - Called with the id of a choice.
 * @param props.className - The class of the menu.
 * @param props.note - A line under the choices, if any.
 * @returns The menu.
 */
export function ChoiceMenu<T extends string>({
  label,
  choices,
  selected,
  onChoose,
  className = "sort-select",
  note,
}: ChoiceMenuProps<T>): JSX.Element {
  const noteId = useId();
  return (
    <Select
      className={className}
      value={selected}
      onChange={(key) => {
        // The only keys are those of the choices.
        onChoose(String(key) as T);
      }}
    >
      <Label>{label}</Label>
      <Button className="button">
        <SelectValue />
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom end">
        <ListBox className="menu" {...(note === undefined ? {} : { "aria-describedby": noteId })}>
          {choices.map((choice) => (
            <ListBoxItem
              key={choice.id}
              id={choice.id}
              className="menu-item"
              textValue={choice.text}
            >
              <span className="menu-check" aria-hidden="true" />
              {choice.text}
            </ListBoxItem>
          ))}
        </ListBox>
        {note !== undefined && (
          <p className="menu-foot menu-note" id={noteId}>
            {note}
          </p>
        )}
      </Popover>
    </Select>
  );
}
