import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import { INSTANCE_GLYPHS, InstanceState, statusSentences } from "./instanceStatus";

const i18n = initI18n();

describe("instanceStatus", () => {
  it.each([
    ["healthy", "succeeded", "Healthy"],
    ["degraded", "warning", "Degraded"],
    ["down", "failed", "Down"],
  ] as const)("draws %s with its glyph and word", (state, glyph, word) => {
    const { container } = render(<InstanceState state={state} />);

    expect(INSTANCE_GLYPHS[state]).toBe(glyph);
    expect(container.querySelector(`.status-glyph-${glyph}`)).not.toBeNull();
    expect(container).toHaveTextContent(word);
  });

  it("counts the lost workers in the worker_lost sentence", () => {
    const status = {
      state: "degraded",
      causes: ["worker_lost"],
      workers_alive: 1,
      workers_total: 4,
      detail: null,
    } as const;

    expect(statusSentences(i18n.t, status)).toEqual([
      "Queries are slower: 3 of 4 workers are down.",
    ]);
  });
});
