import { describe, expect, it } from "vitest";

import { isNewQueryKey } from "./newQueryKeys";

/**
 * A key press with Alt and Shift held.
 *
 * @param over - What differs.
 * @returns The key press.
 */
function press(over: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { altKey: true, shiftKey: true, ...over });
}

describe("isNewQueryKey", () => {
  it("takes Alt Shift N, by its letter or, where Option changed the letter, by its place", () => {
    expect(isNewQueryKey(press({ key: "N", code: "KeyN" }))).toBe(true);
    expect(isNewQueryKey(press({ key: "n", code: "KeyB" }))).toBe(true);
    expect(isNewQueryKey(press({ key: "˜", code: "KeyN" }))).toBe(true);
  });

  it("leaves another letter at N's place alone, as on a Dvorak layout", () => {
    expect(isNewQueryKey(press({ key: "B", code: "KeyN" }))).toBe(false);
  });

  it("leaves N alone without both Alt and Shift, or with Ctrl or ⌘", () => {
    expect(isNewQueryKey(press({ key: "N", code: "KeyN", altKey: false }))).toBe(false);
    expect(isNewQueryKey(press({ key: "n", code: "KeyN", shiftKey: false }))).toBe(false);
    expect(isNewQueryKey(press({ key: "Ń", code: "KeyN", ctrlKey: true }))).toBe(false);
    expect(isNewQueryKey(press({ key: "N", code: "KeyN", metaKey: true }))).toBe(false);
  });
});
