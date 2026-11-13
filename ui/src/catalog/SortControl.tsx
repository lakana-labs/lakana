/**
 * The "Sort by" and "Order" menus at the top right of the list: every column, and Created,
 * which is not a column.
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

import { TABLE_SORTS } from "../api";
import type { SortOrder } from "../api";
import { type CatalogView, defaultOrder } from "./view";

/** One choice of a menu. */
interface Choice<T extends string> {
  /** What the choice sets. */
  readonly id: T;
  /** The words it is written as. */
  readonly text: string;
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
 * @returns The menu.
 */
function Menu<T extends string>({
  label,
  choices,
  selected,
  onChoose,
}: {
  readonly label: string;
  readonly choices: readonly Choice<T>[];
  readonly selected: T;
  readonly onChoose: (id: T) => void;
}): JSX.Element {
  return (
    <Select
      className="sort-select"
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
        <ListBox className="menu">
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
      </Popover>
    </Select>
  );
}

/** Properties of {@link SortControl}. */
export interface SortControlProps {
  /** The view whose sort is shown. */
  readonly view: CatalogView;
  /**
   * Called with the view after a choice: another key starts in its own default order, another
   * order keeps the key.
   */
  readonly onChange: (view: CatalogView) => void;
}

/**
 * Renders the two menus; the order is named in words that fit the key ("Newest first"), the
 * key's own direction first.
 *
 * @param props - The view and what to do with a choice.
 * @param props.view - The view whose sort is shown.
 * @param props.onChange - Called with the view after a choice.
 * @returns The menus.
 */
export function SortControl({ view, onChange }: SortControlProps): JSX.Element {
  const { t } = useTranslation();
  const first = defaultOrder(view.sort);
  const orders: readonly SortOrder[] = first === "asc" ? ["asc", "desc"] : ["desc", "asc"];
  return (
    <div className="sort-control">
      <Menu
        label={t("catalog.sortBy")}
        choices={TABLE_SORTS.map((sort) => ({ id: sort, text: t(`catalog.columns.${sort}`) }))}
        selected={view.sort}
        onChoose={(sort) => {
          onChange({ ...view, sort, order: defaultOrder(sort) });
        }}
      />
      <Menu
        label={t("catalog.order")}
        choices={orders.map((order) => ({
          id: order,
          text: t(`catalog.orders.${view.sort}.${order}`),
        }))}
        selected={view.order}
        onChoose={(order) => {
          onChange({ ...view, order });
        }}
      />
    </div>
  );
}
