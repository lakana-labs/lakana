/**
 * Helpers for the tests of a list that sorts by its columns.
 *
 * @packageDocumentation
 */
import { within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/**
 * The text of one column of a list, its rows in the order shown.
 *
 * @param scope - The list, or a region or panel that holds only it.
 * @param index - The column, from 0.
 * @returns Each body row's text in that column.
 */
export function columnTexts(scope: HTMLElement, index = 0): (string | null)[] {
  return within(scope)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[index]?.textContent ?? null);
}

/**
 * Presses a column's header, which sorts the list by it (pressed again, the other way).
 *
 * @param user - The user-event session.
 * @param scope - The list, or what holds it.
 * @param column - The column's name.
 */
export async function sortBy(user: UserEvent, scope: HTMLElement, column: string): Promise<void> {
  await user.click(within(scope).getByRole("button", { name: column }));
}

/**
 * The sorted column's state, as `aria-sort` says it.
 *
 * @param scope - The list, or what holds it.
 * @param column - The column's name.
 * @returns `ascending`, `descending` or null.
 */
export function sortState(scope: HTMLElement, column: string): string | null {
  return within(scope).getByRole("columnheader", { name: column }).getAttribute("aria-sort");
}
