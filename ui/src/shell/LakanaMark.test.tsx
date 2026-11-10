import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { luminance, ratio } from "../test/palette";
import { LakanaMark } from "./LakanaMark";

const UI = resolve(import.meta.dirname, "../..");
const MARK_FILE = readFileSync(resolve(import.meta.dirname, "lakana-mark.svg"), "utf8");
const FAVICON = readFileSync(resolve(UI, "public/favicon.svg"), "utf8");
const INDEX = readFileSync(resolve(UI, "index.html"), "utf8");

/**
 * The fills an SVG file declares.
 *
 * @param svg - The file's text.
 * @returns Every `fill` attribute value, in order.
 */
function fills(svg: string): string[] {
  return [...svg.matchAll(/\bfill="([^"]*)"/g)].map((match) => match[1] ?? "");
}

/**
 * The path data of an SVG file with one path.
 *
 * @param svg - The file's text.
 * @returns The `d` attribute of its only path.
 */
function pathData(svg: string): string {
  const paths = [...svg.matchAll(/<path\b[^>]*\bd="([^"]*)"/g)];
  expect(paths).toHaveLength(1);
  return paths[0]?.[1] ?? "";
}

describe("LakanaMark", () => {
  it("draws one shape in currentColor, hidden from assistive technology by default", () => {
    const { container } = render(<LakanaMark />);
    const svg = container.querySelector("svg");

    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("viewBox", "0 0 16 16");
    expect(svg).toHaveAttribute("width", "16");
    expect(container.querySelectorAll("path")).toHaveLength(1);
    expect(container.querySelector("path")).toHaveAttribute("fill", "currentColor");
  });

  it("is announced with its label when it stands alone", () => {
    render(<LakanaMark label="Lakana" size={32} />);

    const mark = screen.getByRole("img", { name: "Lakana" });
    expect(mark).toHaveAttribute("width", "32");
    expect(mark).not.toHaveAttribute("aria-hidden");
  });

  it("is the same shape as the mark file and the favicon", () => {
    const { container } = render(<LakanaMark />);
    const drawn = container.querySelector("path")?.getAttribute("d");

    expect(pathData(MARK_FILE)).toBe(drawn);
    expect(pathData(FAVICON)).toBe(drawn);
  });

  it("keeps the mark file to a single currentColor fill", () => {
    expect(fills(MARK_FILE)).toEqual(["currentColor"]);
    expect(MARK_FILE).not.toMatch(/<style|style=|<script|<image|href=/);
  });

  it("gives the favicon one fixed colour that reads on light and dark browser tabs", () => {
    const [colour = ""] = fills(FAVICON);

    expect(fills(FAVICON)).toHaveLength(1);
    expect(colour).toMatch(/^#[0-9a-f]{6}$/);
    expect(FAVICON).not.toMatch(/<style|style=|<script|<image|href=/);
    // A light tab, and the darkest and lightest dark tab strips of current browsers.
    for (const tab of ["#ffffff", "#202124", "#35363a"]) {
      expect(ratio(luminance(colour), luminance(tab)), tab).toBeGreaterThanOrEqual(3);
    }
  });

  it("links the SVG favicon and its PNG fallback from index.html", () => {
    expect(INDEX).toContain(
      '<link rel="icon" href="/favicon.png" sizes="32x32" type="image/png" />',
    );
    expect(INDEX).toContain('<link rel="icon" href="/favicon.svg" type="image/svg+xml" />');
  });

  it("ships a 32 × 32 PNG fallback", () => {
    const png = readFileSync(resolve(UI, "public/favicon.png"));

    expect(png.subarray(1, 4).toString("ascii")).toBe("PNG");
    expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([32, 32]);
  });
});
