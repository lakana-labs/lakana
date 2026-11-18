import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { aFailure } from "../test/jobs";
import { DOCS_URL } from "../shell/docs";
import { FailureLine } from "./FailureLine";

describe("FailureLine", () => {
  it("shows the code, the cause and the next step", () => {
    render(<FailureLine failure={aFailure("build_failed")} />);

    const line = screen.getByText(/Cause:/).closest("p");
    expect(line).toHaveTextContent(
      "build_failed (opens in a new tab) · Cause: The task failed with build_failed. Next step: Fix the cause and resume the run.",
    );
    const code = screen.getByRole("link", { name: /build_failed/ });
    expect(code).toHaveAttribute("href", `${DOCS_URL}reference/why-did-it-fail/#the-codes`);
    expect(code).toHaveAttribute("rel", "noopener noreferrer");
    expect(code).toHaveAttribute("target", "_blank");
  });

  it.each(["unknown", "memory_spike"])(
    "shows an unknown code as internal with the servers sentences (%s)",
    (code) => {
      render(<FailureLine failure={aFailure(code)} />);

      expect(screen.getByRole("link", { name: /internal/ })).toBeInTheDocument();
      expect(screen.queryByText(code, { exact: false })).not.toHaveTextContent(`${code} (`);
      const line = screen.getByText(/Cause:/).closest("p");
      expect(line).toHaveTextContent(
        `Cause: The task failed with ${code}. Next step: Fix the cause`,
      );
      expect(line).toHaveTextContent(/^internal/);
    },
  );

  it("marks a hidden character in the cause and the next step", () => {
    render(
      <FailureLine
        failure={{ code: "timeout", cause: "It ran‮ long.", next_step: "Raise​ it." }}
      />,
    );

    const line = screen.getByText(/Cause:/).closest("p");
    expect(line).toHaveTextContent("Cause: It ranU+202E long. Next step: RaiseU+200B it.");
  });
});
