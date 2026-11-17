import { Text } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { errorDiagnostic, errorPosition } from "./errorMark";

const doc = Text.of(["select *", "from gold.station_ranking", "where 𝔸 = $count("]);

/**
 * The text a diagnostic marks.
 *
 * @param from - Its start.
 * @param to - Its end.
 * @returns The marked text.
 */
function marked(from: number, to: number): string {
  return doc.sliceString(from, to);
}

describe("errorDiagnostic", () => {
  it("marks from the column to the end of the word", () => {
    const word = errorDiagnostic(doc, 2, 6, "Table gold.station_ranking does not exist.");

    expect(marked(word.from, word.to)).toBe("gold");
    const last = errorDiagnostic(doc, 2, 11, "Table station_ranking does not exist.");
    expect(marked(last.from, last.to)).toBe("station_ranking");
    expect(word).toMatchObject({
      severity: "error",
      message: "Table gold.station_ranking does not exist.",
    });
    // Columns count code points: 𝔸 is one column but two UTF-16 units.
    const after = errorDiagnostic(doc, 3, 11, "Unknown parameter.");
    expect(marked(after.from, after.to)).toBe("$count");
    // Where no word starts, the one character there.
    const bracket = errorDiagnostic(doc, 3, 17, "Unclosed bracket.");
    expect(marked(bracket.from, bracket.to)).toBe("(");
  });

  it("clamps a position past the end", () => {
    const past = errorDiagnostic(doc, 1, 40, "Expected FROM.");

    expect(past.from).toBe(doc.line(1).to);
    expect(past.to).toBe(doc.line(1).to);
    const before = errorDiagnostic(doc, 0, 0, "Unexpected text.");
    expect(marked(before.from, before.to)).toBe("select");
  });

  it("a line past the last marks the last line", () => {
    const last = errorDiagnostic(doc, 9, 1, "Unexpected end of input.");

    expect(marked(last.from, last.to)).toBe("where");
    expect(doc.lineAt(last.from).number).toBe(3);
  });
});

describe("errorPosition", () => {
  it("is the offset of a line and a column, both from 1", () => {
    expect(errorPosition(doc, 2, 6)).toBe(doc.line(2).from + 5);
  });
});
