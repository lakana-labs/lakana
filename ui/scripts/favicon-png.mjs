// Renders public/favicon.svg into public/favicon.png, the 32 × 32 fallback for browsers without
// SVG favicons. Run it after changing the mark (`node scripts/favicon-png.mjs`);
// e2e/mark.spec.ts fails while the PNG differs from a fresh rendering.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const PUBLIC = resolve(dirname(fileURLToPath(import.meta.url)), "../public");
const SIZE = 32;

const svg = readFileSync(resolve(PUBLIC, "favicon.svg"), "utf8");
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const url = await page.evaluate(
    async ({ markup, size }) => {
      const image = new Image(size, size);
      image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      canvas.getContext("2d")?.drawImage(image, 0, 0, size, size);
      return canvas.toDataURL("image/png");
    },
    { markup: svg, size: SIZE },
  );
  writeFileSync(resolve(PUBLIC, "favicon.png"), Buffer.from(url.split(",")[1] ?? "", "base64"));
} finally {
  await browser.close();
}
