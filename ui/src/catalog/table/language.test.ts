import { describe, expect, it } from "vitest";

import { table } from "../../test/table";
import { editLanguage, languageName } from "./language";

const GERMAN = [{ language: "de", translated: 3, total: 5 }];

describe("editLanguage", () => {
  it("edits the language of the text the page shows", () => {
    const shown = table({ language: "de", main_language: "en", translations: GERMAN });

    expect(editLanguage(shown, { description_language: "de" })).toEqual({
      lang: "de",
      named: true,
      fallback: false,
    });
  });

  it("falls back to the main language when the page shows it for want of a translation", () => {
    const shown = table({ language: "de", main_language: "en", translations: [] });

    expect(editLanguage(shown, { description_language: "en" })).toEqual({
      lang: "en",
      named: false,
      fallback: true,
    });
  });

  it("names the language of a text shown in the main language when the table has translations", () => {
    const shown = table({ language: "en", main_language: "en", translations: GERMAN });

    expect(editLanguage(shown, { description_language: "en" })).toEqual({
      lang: "en",
      named: true,
      fallback: false,
    });
  });

  it("does not name the language of a table with one language", () => {
    expect(editLanguage(table(), { description_language: "en" })).toEqual({
      lang: "en",
      named: false,
      fallback: false,
    });
  });

  it("writes a first text in the reader's language when the table has texts in it", () => {
    const shown = table({ language: "de", main_language: "en", translations: GERMAN });

    expect(editLanguage(shown, { description_language: null }).lang).toBe("de");
  });

  it("writes a first text in the main language when the table has none in the reader's", () => {
    const shown = table({ language: "fr", main_language: "en", translations: GERMAN });

    expect(editLanguage(shown, { description_language: null })).toEqual({
      lang: "en",
      named: true,
      fallback: true,
    });
  });
});

describe("languageName", () => {
  it("names a language in the interface's language", () => {
    expect(languageName("de", "en")).toBe("German");
    expect(languageName("de", "de")).toBe("Deutsch");
  });

  it("returns the tag for one the browser does not know", () => {
    expect(languageName("not a tag!", "en")).toBe("not a tag!");
  });
});
