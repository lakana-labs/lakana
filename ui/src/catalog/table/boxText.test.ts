import { act, renderHook } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { assetNode, graphNode } from "../../test/table";
import {
  boxLines,
  boxWidth,
  estimateWidth,
  fitText,
  TEXT_X,
  textMeasure,
  useTextMeasure,
} from "./boxText";

const t = i18next.t.bind(i18next);

/**
 * Ten pixels per character, so the numbers are easy to follow.
 *
 * @param text - The line.
 * @returns Its width.
 */
const tens = (text: string): number => text.length * 10;

describe("fitText", () => {
  it("keeps a line that fits whole", () => {
    expect(fitText("abcdef", 60, tens)).toBe("abcdef");
  });

  it("cuts between whole characters and never inside a mark", () => {
    // A rocket is two UTF-16 units, a flag two code points: neither is split.
    const graphemes = (text: string): number =>
      [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)].length * 10;
    expect(fitText("ab🚀cd", 45, graphemes)).toBe("ab🚀…");
    expect(
      fitText("ab🇩🇪cd", 35, (text) => [...new Intl.Segmenter().segment(text)].length * 10),
    ).toBe("ab…");
    // "xU+202Ey": the mark is one unit, kept whole or left out whole.
    expect(fitText("xU+202Eyz", 50, tens)).toBe("x…");
    expect(fitText("xU+200B ×3yz", 50, tens)).toBe("x…");
    expect(fitText("xU+202Eyz", 85, tens)).toBe("xU+202E…");
  });

  it("ends a line that does not fit in an ellipsis, inside the room", () => {
    expect(fitText("abcdef", 45, tens)).toBe("abc…");
    expect(fitText("abcdef", 5, tens)).toBe("…");
  });
});

describe("boxLines", () => {
  it("writes a table as its name and its namespace", () => {
    expect(boxLines(t, { node: graphNode("table", "gold.sales.fact_sales", 1), jobs: [] })).toEqual(
      ["fact_sales", "gold.sales"],
    );
    expect(boxLines(t, { node: graphNode("table", "loose", 1), jobs: [] })).toEqual([
      "loose",
      null,
    ]);
  });

  it("writes an asset as its jobs, then its name and repository", () => {
    const asset = assetNode("gold.sales.fact_sales", 1);
    const job = graphNode("job", "analytics/daily_sales", 1);
    expect(boxLines(t, { node: asset, jobs: [job] })).toEqual([
      "Job daily_sales",
      "asset gold.sales.fact_sales · analytics",
    ]);
    const bare = graphNode("asset", "x‮y", 1);
    expect(boxLines(t, { node: bare, jobs: [] })).toEqual(["Run by no job", "asset xU+202Ey"]);
  });

  it("keeps a name that holds the text of a placeholder as it is", () => {
    const asset = assetNode("x<repository/>y", 1, "r<name/>s");
    expect(boxLines(t, { node: asset, jobs: [] })[1]).toBe("asset x<repository/>y · r<name/>s");
  });

  it("writes a connection as its name and its kind", () => {
    const known = graphNode("connection", "pg-sales", 1, { connection_kind: "postgresql" });
    const odd = graphNode("connection", "odd", 1, { connection_kind: "new‮kind" });
    expect(boxLines(t, { node: known, jobs: [] })).toEqual(["pg-sales", "PostgreSQL"]);
    expect(boxLines(t, { node: odd, jobs: [] })).toEqual(["odd", "newU+202Ekind"]);
  });
});

describe("boxWidth", () => {
  it("is the longest line and the space on both sides", () => {
    const node = graphNode("table", "gold.sales.fact_sales", 1);
    expect(boxWidth(t, { node, jobs: [] }, tens)).toBe(10 * 10 + 2 * TEXT_X);
    const loose = graphNode("table", "loose", 1);
    expect(boxWidth(t, { node: loose, jobs: [] }, tens)).toBe(5 * 10 + 2 * TEXT_X);
  });
});

describe("textMeasure", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.style.removeProperty("--font-sans");
  });

  it("estimates the width where no canvas can measure text", () => {
    vi.stubGlobal("OffscreenCanvas", undefined);
    expect(textMeasure()("abcd")).toBe(estimateWidth("abcd"));
    expect(estimateWidth("abcd")).toBeGreaterThan(0);
  });

  it("measures with the page's font where a canvas can", () => {
    const fonts: string[] = [];
    const context = {
      set font(value: string) {
        fonts.push(value);
      },
      measureText: (text: string) => ({ width: text.length * 3 }),
    };
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext(): unknown {
          return context;
        }
      },
    );
    document.documentElement.style.setProperty("--font-sans", '"IBM Plex Sans", sans-serif');
    expect(textMeasure()("abcd")).toBe(12);
    expect(fonts).toEqual(['12.5px "IBM Plex Sans", sans-serif']);
  });

  it("measures a mark in the mono font it is drawn in", () => {
    const widths: Record<string, number> = { sans: 1, mono: 3 };
    let font = "";
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext(): unknown {
          return {
            set font(value: string) {
              font = value;
            },
            measureText: (text: string) => ({
              width:
                text.length * (font.includes("Mono") ? (widths.mono ?? 0) : (widths.sans ?? 0)),
            }),
          };
        }
      },
    );
    document.documentElement.style.setProperty("--font-sans", '"IBM Plex Sans"');
    document.documentElement.style.setProperty("--font-mono", '"IBM Plex Mono"');
    // "ab" in sans (2), "U+202E" in mono (6 × 3), "c" in sans (1).
    expect(textMeasure()("abU+202Ec")).toBe(2 + 18 + 1);
    document.documentElement.style.removeProperty("--font-mono");
  });

  it("measures with a plain sans-serif before the page's tokens are known", () => {
    const fonts: string[] = [];
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext(): unknown {
          return {
            set font(value: string) {
              fonts.push(value);
            },
            measureText: () => ({ width: 1 }),
          };
        }
      },
    );
    expect(textMeasure()("a")).toBe(1);
    expect(fonts).toEqual(["12.5px sans-serif"]);
  });

  it("estimates when the canvas has no 2D context", () => {
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext(): null {
          return null;
        }
      },
    );
    expect(textMeasure()("abcd")).toBe(estimateWidth("abcd"));
  });
});

describe("useTextMeasure", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    Reflect.deleteProperty(document, "fonts");
  });

  it("estimates where the page knows no fonts", () => {
    const { result } = renderHook(() => useTextMeasure());
    expect(result.current("abcd")).toBe(estimateWidth("abcd"));
  });

  it("measures again once the fonts have loaded", async () => {
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext(): unknown {
          return { font: "", measureText: () => ({ width: 1 }) };
        }
      },
    );
    let loaded = (): void => undefined;
    const ready = new Promise<void>((resolve) => {
      loaded = resolve;
    });
    Object.defineProperty(document, "fonts", { value: { ready }, configurable: true });
    const { result } = renderHook(() => useTextMeasure());
    const before = result.current;

    await act(async () => {
      loaded();
      await ready;
    });

    expect(result.current).not.toBe(before);
  });

  it("does nothing when the fonts load after the drawing has gone", async () => {
    let loaded = (): void => undefined;
    const ready = new Promise<void>((resolve) => {
      loaded = resolve;
    });
    Object.defineProperty(document, "fonts", { value: { ready }, configurable: true });
    const { result, unmount } = renderHook(() => useTextMeasure());
    const before = result.current;
    unmount();

    await act(async () => {
      loaded();
      await ready;
    });

    expect(result.current).toBe(before);
  });
});
