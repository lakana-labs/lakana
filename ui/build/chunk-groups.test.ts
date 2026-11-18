import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { FORM_CHUNK, SHELL_CHUNK } from "./chunk-groups";

const ROOT = resolve(import.meta.dirname, "..");

describe("the shell chunk group", () => {
  it.each([
    "src/shell/Page.tsx",
    "src/shell/NotFound.tsx",
    "node_modules/react-aria-components/dist/private/Button.mjs",
  ])("matches a file that exists (%s)", (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
    expect(SHELL_CHUNK.test(resolve(ROOT, file))).toBe(true);
  });

  it("matches Windows-style paths and nothing else of the shell", () => {
    expect(SHELL_CHUNK.test("C:\\app\\src\\shell\\Page.tsx")).toBe(true);
    expect(SHELL_CHUNK.test(resolve(ROOT, "src/shell/AppShell.tsx"))).toBe(false);
  });
});

describe("the form chunk group", () => {
  it.each([
    "node_modules/react-aria-components/dist/private/FieldError.mjs",
    "node_modules/react-aria-components/dist/private/Form.mjs",
    "node_modules/react-aria-components/dist/private/Group.mjs",
    "node_modules/react-aria-components/dist/private/Input.mjs",
    "node_modules/react-stately/dist/private/form/useFormValidationState.mjs",
    "node_modules/react-aria/dist/private/label/useField.mjs",
    "node_modules/react-aria/dist/private/utils/useFormReset.mjs",
    "node_modules/react-aria/dist/private/form/useFormValidation.mjs",
    "node_modules/react-aria/dist/private/textfield/useTextField.mjs",
  ])("matches a file that exists (%s)", (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
    expect(FORM_CHUNK.test(resolve(ROOT, file))).toBe(true);
  });

  it("matches Windows-style paths and none of React Aria's other components", () => {
    expect(
      FORM_CHUNK.test("C:\\app\\node_modules\\react-aria\\dist\\private\\label\\useField.mjs"),
    ).toBe(true);
    for (const file of [
      "node_modules/react-aria-components/dist/private/Select.mjs",
      "node_modules/react-aria-components/dist/private/TextField.mjs",
      "node_modules/react-aria-components/dist/exports/Form.mjs",
    ]) {
      expect(FORM_CHUNK.test(resolve(ROOT, file))).toBe(false);
    }
  });
});
