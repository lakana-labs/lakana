import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { StatusGlyph } from "../src/status/StatusGlyph";
import { MACHADO_2009 } from "../src/test/colour-vision";
import { STATUS_KINDS } from "../src/status/status";

const SRC = resolve(import.meta.dirname, "../src");
const STYLES = ["styles/tokens.css", "status/status.css"]
  .map((file) => readFileSync(resolve(SRC, file), "utf8"))
  .join("\n");

/**
 * An SVG filter that applies one colour-vision matrix (filters work in linear RGB by default,
 * as the matrices expect).
 *
 * @param id - The filter's id.
 * @param matrix - The 3×3 matrix by rows.
 * @returns The markup of a hidden SVG holding the filter.
 */
function visionFilter(id: string, matrix: readonly (readonly number[])[]): string {
  const rows = matrix.map((row) => `${row.join(" ")} 0 0`).join(" ");
  return `<svg width="0" height="0" style="position:absolute"><filter id="${id}" color-interpolation-filters="linearRGB"><feColorMatrix type="matrix" values="${rows} 0 0 0 1 0"/></filter></svg>`;
}

for (const scheme of ["light", "dark"] as const) {
  test(`the seven glyphs at 12 px, in colour, in greyscale and through colour-blindness simulations (${scheme})`, async ({
    page,
    browserName,
  }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in Chromium only");
    await page.emulateMedia({ colorScheme: scheme });
    const glyphs = STATUS_KINDS.map((kind) =>
      renderToStaticMarkup(createElement(StatusGlyph, { kind, size: 12 })),
    ).join("");
    await page.setContent(
      `<style>${STYLES} body{margin:16px;background:var(--color-ground);display:flex;gap:12px}</style>${glyphs}`,
    );

    const boxes = await page.locator("svg").evaluateAll((nodes) =>
      nodes.map((node) => {
        const { width, height } = node.getBoundingClientRect();
        return [width, height];
      }),
    );
    expect(boxes).toEqual(STATUS_KINDS.map(() => [12, 12]));

    await page.screenshot({ path: `test-results/glyphs-12px-${scheme}.png` });
    await page.addStyleTag({ content: "html { filter: grayscale(1) }" });
    await page.screenshot({ path: `test-results/glyphs-12px-${scheme}-grey.png` });
    await page.addStyleTag({ content: "html { filter: none }" });

    // The numbers are checked in src/status/colour-vision.test.ts; these are for the eye.
    for (const [vision, matrix] of Object.entries(MACHADO_2009)) {
      await page.evaluate(
        (markup) => {
          document.body.insertAdjacentHTML("beforeend", markup);
        },
        visionFilter(vision, matrix),
      );
      await page.addStyleTag({ content: `html { filter: url(#${vision}) }` });
      await page.screenshot({ path: `test-results/glyphs-12px-${scheme}-${vision}.png` });
    }
  });
}
