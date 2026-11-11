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
});
