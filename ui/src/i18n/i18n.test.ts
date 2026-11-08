import { expect, it } from "vitest";

import { STATUS_KINDS } from "../status/status";
import { initI18n } from "./i18n";

it("every status kind has an English word", () => {
  const i18n = initI18n();

  for (const kind of STATUS_KINDS) {
    const word = i18n.t(`status.${kind}`);
    expect(word).not.toBe("");
    expect(word).not.toBe(`status.${kind}`);
  }
});
