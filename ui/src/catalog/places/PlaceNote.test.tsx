import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlaceNote } from "./PlaceNote";

describe("PlaceNote", () => {
  it.each([
    ["created", "Created namespace gold.finance."],
    ["deleted", "Deleted namespace gold.finance."],
    [
      "notShown",
      "Created namespace gold.finance. You may not read the namespaces of prod, so the tree does not show it while it holds no table you may read.",
    ],
  ] as const)("says %s in a status line that takes the focus", (placeNote, text) => {
    render(<PlaceNote note={{ placeNote, namespace: "gold.finance", environment: "prod" }} />);

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(text);
    expect(note).toHaveClass("catalog-result");
    expect(note).toHaveAttribute("tabindex", "-1");
    expect(note).toHaveFocus();
  });

  it("shows invisible characters of the names as marks", () => {
    render(
      <PlaceNote
        note={{ placeNote: "notShown", namespace: "gold\u202Efinance", environment: "pr\u200Bod" }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Created namespace goldU+202Efinance. You may not read the namespaces of prU+200Bod,",
    );
  });

  it("says Dropped artifact {artifact} with its files removed, and marks a bidi character in the name", () => {
    render(
      <PlaceNote
        note={{
          placeNote: "artifactDropped",
          namespace: "ml",
          environment: "prod",
          artifact: "ml.mo\u202Edel",
        }}
      />,
    );

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(
      "Dropped artifact ml.moU+202Edel. Its files are removed from storage, and Lakana's periodic clean-up removes any it could not.",
    );
    expect(note).toHaveFocus();
  });
});
