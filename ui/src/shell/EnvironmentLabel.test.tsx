import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EnvironmentLabel } from "./EnvironmentLabel";

describe("EnvironmentLabel", () => {
  it("names the environment, and says so to a screen reader", () => {
    render(<EnvironmentLabel environment="prod" />);

    expect(screen.getByText("prod")).toHaveClass("env-label");
    expect(screen.getByText("prod")).toHaveTextContent("Environment prod");
  });

  it("marks a protected environment as the picker does, in the colour of danger and in words", () => {
    const { container } = render(<EnvironmentLabel environment="prod" protected />);

    const label = container.querySelector(".env-label");
    expect(label).toHaveTextContent("Environment ● prod · protected");
    expect(screen.getByText("protected")).toHaveClass("env-protected");
    expect(screen.getByText("●")).toHaveClass("env-protected");
    expect(screen.getByText("●")).toHaveAttribute("aria-hidden", "true");
  });

  it("names an environment that is not protected plainly", () => {
    const { container } = render(<EnvironmentLabel environment="dev" protected={false} />);

    expect(container.querySelector(".env-label")).toHaveTextContent(/^Environment dev$/u);
    expect(container.querySelector(".env-protected")).toBeNull();
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
