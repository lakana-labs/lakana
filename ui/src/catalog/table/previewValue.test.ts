import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { previewValue } from "./previewValue";

const t = i18next.t.bind(i18next);

describe("previewValue", () => {
  it("writes binary, null, nested and cut values as text", () => {
    expect(previewValue(null, "string", false, t)).toEqual({
      text: "null",
      muted: true,
      cut: false,
    });
    expect(previewValue({ binary: 12 }, "binary", false, t)).toEqual({
      text: "binary, 12 bytes",
      muted: true,
      cut: false,
    });
    expect(previewValue({ binary: 1 }, "BLOB", false, t).text).toBe("binary, 1 byte");
    for (const type of [
      "large_binary",
      "binary_view",
      "fixed_size_binary[16]",
      "bytea",
      "VARBINARY",
    ]) {
      expect(previewValue({ binary: 4 }, type, false, t).text, type).toBe("binary, 4 bytes");
    }
    expect(previewValue([1, [2, 3]], "list<item: int32>", false, t).text).toBe("[1,[2,3]]");
    expect(previewValue({ a: 1, b: "x" }, "struct<a: int32, b: string>", false, t).text).toBe(
      '{"a":1,"b":"x"}',
    );
    expect(previewValue({ cut: true, text: "abc" }, "string", false, t)).toEqual({
      text: "abc",
      muted: false,
      cut: true,
    });
    expect(previewValue({ cut: true }, "int64", false, t)).toEqual({
      text: "",
      muted: false,
      cut: true,
    });
    expect(previewValue("12345678901234567890", "int64", false, t).text).toBe(
      "12345678901234567890",
    );
    expect(previewValue(true, "bool", false, t).text).toBe("true");
    expect(previewValue(1.5, "double", false, t).text).toBe("1.5");
  });

  it("shows a struct with fields named binary and cut as JSON", () => {
    const type = "struct<binary: int32, cut: bool>";
    expect(previewValue({ binary: 3, cut: true }, type, false, t)).toEqual({
      text: '{"binary":3,"cut":true}',
      muted: false,
      cut: false,
    });
    // A struct of one field named `binary` is a struct, not the binary marker.
    expect(previewValue({ binary: 3 }, "struct<binary: int32>", false, t).text).toBe(
      '{"binary":3}',
    );
    // A struct whose only field is `cut` is a cut marker only in a row the page marks as cut.
    expect(previewValue({ cut: true }, "struct<cut: bool>", false, t).text).toBe('{"cut":true}');
    expect(previewValue({ cut: true }, "struct<cut: bool>", true, t).cut).toBe(true);
    expect(previewValue({ cut: true, text: "a" }, "map<string, string>", false, t).cut).toBe(false);
  });

  it("counts a marker only with its exact keys and values", () => {
    expect(previewValue({ cut: false }, "int64", false, t).text).toBe('{"cut":false}');
    expect(previewValue({ cut: true, text: 5 }, "string", false, t).text).toBe(
      '{"cut":true,"text":5}',
    );
    expect(previewValue({ cut: true, text: "a", more: 1 }, "string", false, t).cut).toBe(false);
    expect(previewValue({ binary: "12" }, "binary", false, t).text).toBe('{"binary":"12"}');
    expect(previewValue({ binary: 12, x: 1 }, "binary", false, t).text).toBe('{"binary":12,"x":1}');
  });
});
