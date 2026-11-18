/**
 * The "Sort by" and "Order" menus at the top right of the list: every column, and Created,
 * which is not a column.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { TABLE_SORTS } from "../api";
import type { SortOrder } from "../api";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { type CatalogView, defaultOrder } from "./view";

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
      <ChoiceMenu
        label={t("catalog.sortBy")}
        choices={TABLE_SORTS.map((sort) => ({ id: sort, text: t(`catalog.columns.${sort}`) }))}
        selected={view.sort}
        onChoose={(sort) => {
          onChange({ ...view, sort, order: defaultOrder(sort) });
        }}
      />
      <ChoiceMenu
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
