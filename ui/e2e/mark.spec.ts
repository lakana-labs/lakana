import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { LakanaMark } from "../src/shell/LakanaMark";

const UI = resolve(import.meta.dirname, "..");
const STYLES = readFileSync(resolve(UI, "src/styles/tokens.css"), "utf8");
const FAVICON_SVG = readFileSync(resolve(UI, "public/favicon.svg"), "utf8");
const FAVICON_PNG = readFileSync(resolve(UI, "public/favicon.png")).toString("base64");

/** Pixels (column, row) of the mark at 16 px that must be ink: sail, hull and outrigger float. */
const INK = [
  [8, 6],
  [10, 12],
  [1, 12],
];
/** Pixels at 16 px that must stay empty: between sail and hull, and between float and hull. */
const EMPTY = [
  [8, 9],
  [8, 10],
  [4, 12],
];

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "the mark is rendered in Chromium only");
});

for (const scheme of ["light", "dark"] as const) {
  test(`the mark at 16 and 32 px (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const marks = [16, 32]
      .map((size) => renderToStaticMarkup(createElement(LakanaMark, { size })))
      .join("");
    await page.setContent(
      `<style>${STYLES} body{margin:16px;background:var(--color-surface);color:var(--color-text);display:flex;gap:12px;align-items:end}</style>${marks}`,
    );

    const boxes = await page
      .locator("svg")
      .evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().width));
    expect(boxes).toEqual([16, 32]);
    await page
      .locator("svg")
      .first()
      .screenshot({ path: `test-results/mark-16px-${scheme}.png` });
    await page
      .locator("svg")
      .last()
      .screenshot({ path: `test-results/mark-32px-${scheme}.png` });
  });
}

test("the mark keeps sail, hull and outrigger apart at 16 px", async ({ page }) => {
  const alpha = await page.evaluate(
    async ({ markup, points }) => {
      const image = new Image(16, 16);
      image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = 16;
      canvas.height = 16;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context?.drawImage(image, 0, 0, 16, 16);
      return points.map(([x = 0, y = 0]) => context?.getImageData(x, y, 1, 1).data[3] ?? -1);
    },
    { markup: FAVICON_SVG, points: [...INK, ...EMPTY] },
  );

  const ink = alpha.slice(0, INK.length);
  const empty = alpha.slice(INK.length);
  for (const [index, value] of ink.entries()) {
    expect(value, `ink at ${String(INK[index])}`).toBeGreaterThanOrEqual(200);
  }
  for (const [index, value] of empty.entries()) {
    expect(value, `empty at ${String(EMPTY[index])}`).toBeLessThanOrEqual(50);
  }
});

test("the PNG favicon is the SVG favicon at 32 px", async ({ page }) => {
  const difference = await page.evaluate(
    async ({ svg, png }) => {
      /**
       * Draws an image source at 32 × 32 and reads its pixels.
       *
       * @param src - The image URL.
       * @returns The RGBA bytes.
       */
      async function pixels(src: string): Promise<Uint8ClampedArray> {
        const image = new Image(32, 32);
        image.src = src;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = 32;
        canvas.height = 32;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context?.drawImage(image, 0, 0, 32, 32);
        return context?.getImageData(0, 0, 32, 32).data ?? new Uint8ClampedArray();
      }
      const fresh = await pixels(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`);
      const kept = await pixels(`data:image/png;base64,${png}`);
      let largest = 0;
      for (let index = 0; index < fresh.length; index += 1) {
        largest = Math.max(largest, Math.abs((fresh[index] ?? 0) - (kept[index] ?? 0)));
      }
      return largest;
    },
    { svg: FAVICON_SVG, png: FAVICON_PNG },
  );

  expect(
    difference,
    "public/favicon.png is stale: run node scripts/favicon-png.mjs",
  ).toBeLessThanOrEqual(32);
});
