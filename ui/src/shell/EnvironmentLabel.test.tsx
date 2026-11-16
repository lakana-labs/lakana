import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EnvironmentLabel } from "./EnvironmentLabel";

describe("EnvironmentLabel", () => {
  it("names the environment, and says so to a screen reader", () => {
    render(<EnvironmentLabel environment="prod" />);

    expect(screen.getByText("prod")).toHaveClass("env-label");
    expect(screen.getByText("prod")).toHaveTextContent("Environment prod");
  });

  it("names the instance for something outside every environment", () => {
    render(<EnvironmentLabel environment={null} />);

    expect(screen.getByText("Instance")).toHaveClass("env-label");
  });

  it("marks hidden characters in the environment's name", () => {
    const { container } = render(<EnvironmentLabel environment={"pr\u202Eod"} />);

    expect(container).toHaveTextContent("Environment prU+202Eod");
    expect(container.textContent).not.toContain("\u202E");
    expect(screen.getByText("U+202E")).toHaveClass("char-mark");
  });
});
