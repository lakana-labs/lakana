import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { QUERY_ENVIRONMENTS } from "../test/queries";
import { EnvironmentPicker } from "./EnvironmentPicker";

describe("EnvironmentPicker", () => {
  it("lists only the given environments, protected ones with the dot and the word protected", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="dev" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Environment: dev" }));
    const options = screen.getAllByRole("option");

    expect(options.map((option) => option.textContent)).toEqual(["dev", "●prod· protected"]);
    expect(within(options[1] as HTMLElement).getByText("●")).toHaveAttribute("aria-hidden", "true");
    await user.click(options[1] as HTMLElement);
    expect(onChange).toHaveBeenCalledWith("prod");
  });

  it("the trigger's name says protected", () => {
    render(<EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="prod" onChange={vi.fn()} />);

    const trigger = screen.getByRole("button", { name: "Environment: prod, protected" });
    expect(trigger).toHaveTextContent("●prod· protected");
    expect(screen.getByText("Environment")).toBeInTheDocument();
  });

  it("the mark is the bad colour token and the word, not the colour alone", () => {
    const { container } = render(
      <EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="prod" onChange={vi.fn()} />,
    );

    const marks = container.querySelectorAll(".env-protected");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveTextContent("●");
    expect(marks[1]).toHaveTextContent("· protected");
    // Vitest hands CSS imports back empty, so the stylesheet is read directly.
    const css = readFileSync(resolve(import.meta.dirname, "queries.css"), "utf8");
    expect(css).toMatch(/\.env-protected\s*{[^}]*color:\s*var\(--color-bad\);/);
  });

  it("names an environment that is no longer listed, such as an opened run's, without a mark", () => {
    const { container } = render(
      <EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="staging" onChange={vi.fn()} />,
    );

    expect(screen.getByRole("button", { name: "Environment: staging" })).toBeInTheDocument();
    expect(container.querySelector(".env-protected")).toBeNull();
  });

  it("marks a hidden character of an environment's name", () => {
    render(
      <EnvironmentPicker
        environments={[{ name: "pr\u202Eod", protected: false }]}
        value={"pr\u202Eod"}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Environment: prU+202Eod" })).toBeInTheDocument();
  });
});
