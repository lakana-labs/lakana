import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { INVISIBLE, markInvisible, VisibleText, VisibleTspan } from "./VisibleText";

/** C0 without tab and line feed, DEL and C1. */
const CONTROLS = [
  ...Array.from({ length: 0x09 }, (_, index) => index),
  ...Array.from({ length: 0x20 - 0x0b }, (_, index) => 0x0b + index),
  ...Array.from({ length: 0xa0 - 0x7f }, (_, index) => 0x7f + index),
];

/** Line and paragraph separator, and format characters (`Cf`), astral ones included. */
const SEPARATORS_AND_FORMAT = [
  0x2028, 0x2029, 0x00ad, 0x061c, 0x200b, 0x200c, 0x200d, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c,
  0x202d, 0x202e, 0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xfeff, 0xfff9, 0x1d173, 0xe0001, 0xe0041,
];

/**
 * The mark a code point is shown as.
 *
 * @param code - The code point.
 * @returns Its `U+XXXX` name.
 */
function markOf(code: number): string {
  return `U+${code.toString(16).toUpperCase().padStart(4, "0")}`;
}

describe("VisibleText", () => {
  it("keeps tabs and line breaks and marks everything else in the set", () => {
    const codes = [...CONTROLS, ...SEPARATORS_AND_FORMAT];
    const text = `\t${codes.map((code) => `a${String.fromCodePoint(code)}`).join("")}\nz`;

    const { container } = render(<VisibleText text={text} />);

    const marks = [...container.querySelectorAll(".char-mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(codes.map(markOf));
    const shown = container.textContent;
    expect(shown.startsWith("\t")).toBe(true);
    expect(shown.endsWith("\nz")).toBe(true);
    for (const code of codes) {
      expect(shown).not.toContain(String.fromCodePoint(code));
    }
  });

  it("marks characters side by side, at the start and at the end", () => {
    const { container } = render(<VisibleText text={"\u202E\u2066ab\u0007"} />);

    expect(container.querySelectorAll(".char-mark")).toHaveLength(3);
    expect(container.textContent).toBe("U+202EU+2066abU+0007");
  });

  it("collapses a run of one character into one mark with its count", () => {
    const { container } = render(<VisibleText text={"\u200B".repeat(300_000)} />);

    expect(container.querySelectorAll(".char-mark")).toHaveLength(1);
    expect(container.textContent).toBe("U+200B \u00D7300000");
  });

  it("keeps a mark for each different character and for each run", () => {
    const { container } = render(<VisibleText text={"a\u200B\u200Bb\u200B\u202E\u202E"} />);

    const marks = [...container.querySelectorAll(".char-mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(["U+200B \u00D72", "U+200B", "U+202E \u00D72"]);
  });

  it("leaves letters of every script, accents and emoji as they are", () => {
    const text = "Café 東京 שלום مرحبا 😀 a b";

    const { container } = render(<VisibleText text={text} />);

    expect(container.textContent).toBe(text);
    expect(container.querySelector(".char-mark")).toBeNull();
  });

  it("shows markup as the characters it is made of", () => {
    const { container } = render(<VisibleText text={"<b>bold</b>\u202E<i>x</i>"} />);

    expect(container.querySelector("b, i")).toBeNull();
    expect(container.textContent).toBe("<b>bold</b>U+202E<i>x</i>");
    expect(container.querySelector(".char-mark")?.children).toHaveLength(0);
  });

  it("matches with the same set everywhere, never remembering the last match", () => {
    expect(INVISIBLE.flags).toBe("gu");
    expect("a\u202Eb\u0007".match(INVISIBLE)).toEqual(["\u202E", "\u0007"]);
    expect("a\u202Eb\u0007".match(INVISIBLE)).toEqual(["\u202E", "\u0007"]);
    expect("\t\n".match(INVISIBLE)).toBeNull();
  });
});

describe("markInvisible", () => {
  it("splits text into plain parts and marks, a run of one character as one mark", () => {
    expect(markInvisible("a\u202E\u202Eb\u0007")).toEqual([
      "a",
      { mark: "U+202E \u00D72" },
      "b",
      { mark: "U+0007" },
    ]);
    expect(markInvisible("")).toEqual([]);
  });
});

describe("VisibleTspan", () => {
  it("marks a bidi and a control character in an SVG name", () => {
    const { container } = render(
      <svg>
        <text>
          <VisibleTspan text={"a\u202Eb\u0007c<b>x</b>"} />
        </text>
      </svg>,
    );

    const marks = [...container.querySelectorAll("tspan.char-mark")];
    expect(marks.map((mark) => mark.textContent)).toEqual(["U+202E", "U+0007"]);
    expect(marks.every((mark) => mark.namespaceURI === "http://www.w3.org/2000/svg")).toBe(true);
    const shown = container.querySelector("text")?.textContent;
    expect(shown).toBe("aU+202EbU+0007c<b>x</b>");
    expect(container.querySelector("text b")).toBeNull();
  });
});
