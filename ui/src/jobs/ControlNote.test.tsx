import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { ControlNote } from "./ControlNote";

describe("ControlNote", () => {
  it("says what a control did as a status, with its link", () => {
    render(
      <MemoryRouter>
        <ControlNote
          note={{ kind: "waits", text: "The start waits: busy.", to: "/jobs/prod/runs/r1" }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("The start waits: busy.");
    expect(screen.getByRole("link", { name: "Open the run it waits for" })).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r1",
    );
  });

  it("says a refusal as an alert, and keeps an empty status without a note", () => {
    const { rerender } = render(<ControlNote note={{ kind: "refused", text: "Not \u202Eyou." }} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Not U+202Eyou.");
    expect(screen.queryByRole("status")).toBeNull();

    rerender(<ControlNote note={null} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});
