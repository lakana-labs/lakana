/**
 * Colour-vision deficiency simulation for the glyph checks.
 *
 * @remarks
 * The dichromacy matrices are those of Machado, Oliveira and Fernandes, "A Physiologically-based
 * Model for Simulation of Color Vision Deficiency", IEEE Transactions on Visualization and
 * Computer Graphics 15(6), 2009, pp. 1291-1298, at severity 1.0 (protanopia, deuteranopia,
 * tritanopia). They act on linear-light RGB, so a colour is linearised first and each result
 * channel clamped to 0..1. Achromatopsia is modelled as the colour's relative luminance on all
 * three channels, which is what a greyscale screenshot shows.
 * @packageDocumentation
 */
import type { LinearRgb } from "./palette";
import { linear, luminanceOf } from "./palette";

/** A 3×3 matrix by rows. */
type Matrix = readonly [LinearRgb, LinearRgb, LinearRgb];

/** The simulated kinds of colour vision, and normal vision. */
export const VISIONS = [
  "normal",
  "protanopia",
  "deuteranopia",
  "tritanopia",
  "achromatopsia",
] as const;

/** One kind of colour vision. */
export type Vision = (typeof VISIONS)[number];

/** Machado et al. (2009), severity 1.0, rows applied to linear RGB. */
export const MACHADO_2009: Readonly<Record<"protanopia" | "deuteranopia" | "tritanopia", Matrix>> =
  {
    protanopia: [
      [0.152286, 1.052583, -0.204868],
      [0.114503, 0.786281, 0.099216],
      [-0.003882, -0.048116, 1.051998],
    ],
    deuteranopia: [
      [0.367322, 0.860646, -0.227968],
      [0.280085, 0.672501, 0.047413],
      [-0.01182, 0.04294, 0.968881],
    ],
    tritanopia: [
      [1.255528, -0.076749, -0.178779],
      [-0.078411, 0.930809, 0.147602],
      [0.004733, 0.691367, 0.3039],
    ],
  };

/**
 * Clamps a channel to 0..1.
 *
 * @param value - The channel.
 * @returns The clamped channel.
 */
function clamp(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/**
 * How a colour looks to someone with the given colour vision.
 *
 * @param hex - The colour, `#RRGGBB`.
 * @param vision - The kind of colour vision.
 * @returns The simulated colour as linear RGB.
 */
export function simulate(hex: string, vision: Vision): LinearRgb {
  const rgb = linear(hex);
  if (vision === "normal") {
    return rgb;
  }
  if (vision === "achromatopsia") {
    const grey = luminanceOf(rgb);
    return [grey, grey, grey];
  }
  const [r, g, b] = rgb;
  const [first, second, third] = MACHADO_2009[vision].map(([x, y, z]) =>
    clamp(x * r + y * g + z * b),
  );
  return [first ?? 0, second ?? 0, third ?? 0];
}
