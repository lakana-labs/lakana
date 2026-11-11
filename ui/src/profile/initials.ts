/**
 * The letters a person's circle shows without a picture.
 *
 * @packageDocumentation
 */

/**
 * Up to two letters, upper case: the first of the first and of the last word of the display
 * name, or else of the principal name split at `.`, `_`, `-` and spaces.
 *
 * @param name - The principal name, such as `kim.lee`.
 * @param displayName - The name to show, when the person has one.
 * @returns The letters, such as `KL`; `?` when the name has none.
 */
export function initials(name: string, displayName: string | null): string {
  const shown = displayName?.trim() ?? "";
  const words = (shown === "" ? name : shown).split(/[\s._-]+/).filter((word) => word !== "");
  if (words.length === 0) {
    return "?";
  }
  return words
    .filter((_word, index) => index === 0 || index === words.length - 1)
    .map((word) => word.replace(/^(.).*$/su, "$1"))
    .join("")
    .toLocaleUpperCase();
}
