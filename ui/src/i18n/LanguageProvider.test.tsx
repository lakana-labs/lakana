import { act, render, screen } from "@testing-library/react";
import type { JSX } from "react";
import { NumberField, Input, Label, useLocale } from "react-aria-components";
import { afterEach, expect, it, vi } from "vitest";

import { initI18n } from "./i18n";
import { LanguageProvider } from "./LanguageProvider";

/**
 * Shows the locale React Aria works in.
 *
 * @returns The locale and its direction.
 */
function Shown(): JSX.Element {
  const { locale, direction } = useLocale();
  return <output data-testid="locale">{`${locale} ${direction}`}</output>;
}

afterEach(() => {
  vi.restoreAllMocks();
});

it("gives React Aria the interface's language, not the browser's", () => {
  vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
  initI18n();

  render(
    <LanguageProvider>
      <Shown />
    </LanguageProvider>,
  );

  expect(screen.getByTestId("locale")).toHaveTextContent("en ltr");
});

it("follows a change of the interface's language", async () => {
  const i18n = initI18n();
  render(
    <LanguageProvider>
      <Shown />
    </LanguageProvider>,
  );

  await act(async () => {
    await i18n.changeLanguage("de");
  });
  expect(screen.getByTestId("locale")).toHaveTextContent("de ltr");
  await act(async () => {
    await i18n.changeLanguage("en");
  });
  expect(screen.getByTestId("locale")).toHaveTextContent("en ltr");
});

it("leaves a whole number in a number field as it was in a German browser", () => {
  vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
  initI18n();

  render(
    <LanguageProvider>
      <NumberField defaultValue={12345} formatOptions={{ useGrouping: false }}>
        <Label>Days</Label>
        <Input />
      </NumberField>
    </LanguageProvider>,
  );

  expect(screen.getByRole("textbox", { name: "Days" })).toHaveValue("12345");
});
