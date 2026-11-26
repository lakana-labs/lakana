import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { ArtifactKeptOut, ArtifactVersionDeletedOut } from "../../api";
import { DELETED } from "../../test/artifacts";
import { deletionWords } from "./deletion";

const t = i18next.t.bind(i18next);

/**
 * What deleting version 3 answers, with the amounts a case gives.
 *
 * @param freed - Files and bytes freed.
 * @param freed.files - How many files.
 * @param freed.bytes - Their size.
 * @param kept - The groups that stayed.
 * @returns The answer.
 */
function answer(
  freed: { files: number; bytes: number },
  kept: readonly ArtifactKeptOut[] = [],
): ArtifactVersionDeletedOut {
  return {
    ...DELETED,
    freed_files: freed.files,
    freed_bytes: freed.bytes,
    kept_files: kept.reduce((sum, group) => sum + group.files, 0),
    kept_bytes: kept.reduce((sum, group) => sum + group.bytes, 0),
    kept,
  };
}

describe("deletionWords", () => {
  it.each([
    [
      "files freed and one kept by another version",
      answer({ files: 2, bytes: 300 }, [
        { reason: "other_version", files: 1, bytes: 100, version: 5 },
      ]),
      "Deleted version 3 of ml.model. Freed 2 files (300 B). Kept 1 file (100 B) that version 5 also uses.",
    ],
    [
      "one file freed and nothing kept",
      answer({ files: 1, bytes: 33 }),
      "Deleted version 3 of ml.model. Freed 1 file (33 B).",
    ],
    [
      "no file freed",
      answer({ files: 0, bytes: 0 }, [
        { reason: "other_version", files: 1, bytes: 33, version: 2 },
      ]),
      "Deleted version 3 of ml.model. Freed no files. Kept 1 file (33 B) that version 2 also uses.",
    ],
    [
      "versions as their numbers, a count for the language",
      {
        ...answer({ files: 12_400, bytes: 33 }, [
          { reason: "other_version", files: 1, bytes: 33, version: 1200 },
        ]),
        version: 1234,
      },
      "Deleted version 1234 of ml.model. Freed 12,400 files (33 B). Kept 1 file (33 B) that version 1200 also uses.",
    ],
    [
      "a version the reader may not see",
      answer({ files: 0, bytes: 0 }, [
        { reason: "other_version", files: 2, bytes: 2_500_000, version: null },
      ]),
      "Deleted version 3 of ml.model. Freed no files. Kept 2 files (2.5 MB) that another version also uses.",
    ],
    [
      "an open upload",
      answer({ files: 1, bytes: 7 }, [
        { reason: "open_upload", files: 1, bytes: 6, version: null },
      ]),
      "Deleted version 3 of ml.model. Freed 1 file (7 B). Kept 1 file (6 B) that an open upload names.",
    ],
    [
      "the periodic clean-up",
      answer({ files: 0, bytes: 0 }, [
        { reason: "left_to_sweep", files: 1200, bytes: 40_000, version: null },
      ]),
      "Deleted version 3 of ml.model. Freed no files. Kept 1,200 files (40 kB) that Lakana's periodic clean-up decides about: it removes what nothing else needs and keeps the rest.",
    ],
    [
      "three reasons, joined as a list",
      answer({ files: 1, bytes: 10 }, [
        { reason: "other_version", files: 1, bytes: 100, version: 5 },
        { reason: "open_upload", files: 2, bytes: 200, version: null },
        { reason: "left_to_sweep", files: 3, bytes: 300, version: null },
      ]),
      "Deleted version 3 of ml.model. Freed 1 file (10 B). Kept 1 file (100 B) that version 5 also uses, 2 files (200 B) that an open upload names, and 3 files (300 B) that Lakana's periodic clean-up decides about: it removes what nothing else needs and keeps the rest.",
    ],
  ])("says what was freed and kept: %s", (_case, deleted, words) => {
    expect(deletionWords(t, "en", "ml.model", deleted)).toBe(words);
  });

  it("reads a reason it does not know as the clean-up's, as the command line does", () => {
    const unknown = { reason: "kept_for_audit", files: 1, bytes: 5, version: null };
    const deleted = answer({ files: 0, bytes: 0 }, [unknown as unknown as ArtifactKeptOut]);
    expect(deletionWords(t, "en", "ml.model", deleted)).toBe(
      "Deleted version 3 of ml.model. Freed no files. Kept 1 file (5 B) that Lakana's periodic clean-up decides about: it removes what nothing else needs and keeps the rest.",
    );
  });

  it("marks a bidi character in the artifact's name", () => {
    const words = deletionWords(t, "en", "ml.mo\u202edel", answer({ files: 0, bytes: 0 }));
    expect(words).not.toContain("\u202e");
    expect(words).toContain("U+202E");
  });
});
