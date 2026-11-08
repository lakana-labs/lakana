import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { StatusGlyph } from "../src/status/StatusGlyph";
import { STATUS_KINDS } from "../src/status/status";

const SRC = resolve(import.meta.dirname, "../src");
const STYLES = ["styles/tokens.css", "status/status.css"]
  .map((file) => readFileSync(resolve(SRC, file), "utf8"))
  .join("\n");

for (const scheme of ["light", "dark"] as const) {
  test(`the seven glyphs at 12 px, in colour and in greyscale (${scheme})`, async ({
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
  });
}
