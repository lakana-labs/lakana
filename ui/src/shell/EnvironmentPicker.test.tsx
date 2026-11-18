import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EnvironmentChoice } from "../api/environments";
import { QUERY_ENVIRONMENTS } from "../test/queries";
import { EnvironmentName, EnvironmentPicker } from "./EnvironmentPicker";

describe("EnvironmentPicker", () => {
  it("lists only the given environments, protected ones with the dot and the word protected", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="dev" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Environment: dev" }));
    const options = screen.getAllByRole("option");

    expect(options.map((option) => option.textContent)).toEqual(["dev", "● prod · protected"]);
    expect(within(options[1] as HTMLElement).getByText("●")).toHaveAttribute("aria-hidden", "true");
    await user.click(options[1] as HTMLElement);
    expect(onChange).toHaveBeenCalledWith("prod");
  });

  it("the trigger's name says protected", () => {
    render(<EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="prod" onChange={vi.fn()} />);

    const trigger = screen.getByRole("button", { name: "Environment: prod, protected" });
    expect(trigger).toHaveTextContent("● prod · protected");
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
    const css = readFileSync(resolve(import.meta.dirname, "environment.css"), "utf8");
    expect(css).toMatch(/\.env-protected\s*{[^}]*color:\s*var\(--color-bad\);/);
  });

  it("names an environment that is no longer listed, such as an opened run's, without a mark", () => {
    const { container } = render(
      <EnvironmentPicker environments={QUERY_ENVIRONMENTS} value="staging" onChange={vi.fn()} />,
    );

    const trigger = screen.getByRole("button", { name: "Environment: staging" });
    // The trigger shows the name, so the person sees where the opened query lives.
    expect(trigger.querySelector(".react-aria-SelectValue")).toHaveTextContent(/^staging$/);
    expect(container.querySelector(".env-protected")).toBeNull();
  });

  it("marks a hidden character of an environment that is no longer listed", () => {
    render(
      <EnvironmentPicker
        environments={QUERY_ENVIRONMENTS}
        value={"stag\u200Bing"}
        onChange={vi.fn()}
      />,
    );

    expect(document.querySelector(".react-aria-SelectValue .char-mark")).toHaveTextContent(
      "U+200B",
    );
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

  it("gives the native select behind it the marked name, never the hidden character", () => {
    const { container } = render(
      <EnvironmentPicker
        environments={[{ name: "pr\u202Eod", protected: false }]}
        value={"pr\u202Eod"}
        onChange={vi.fn()}
      />,
    );

    const options = [...container.querySelectorAll("option")].map((option) => option.textContent);
    expect(options).toContain("prU+202Eod");
    expect(options.join("")).not.toContain("\u202E");
  });
});

describe("EnvironmentPicker over any environment choice", () => {
  it("takes any environment choice, not only a query's", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const choices: readonly EnvironmentChoice[] = [
      { name: "dev", protected: false },
      { name: "prod", protected: true },
    ];
    render(
      <EnvironmentPicker
        environments={choices}
        value={null}
        label="Show"
        all="All environments"
        onChange={onChange}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Show: All environments" }));
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "All environments",
      "dev",
      "● prod · protected",
    ]);
    await user.click(options[2] as HTMLElement);
    expect(onChange).toHaveBeenCalledWith("prod");
  });

  it("names a protected environment with its mark on its own", () => {
    render(<EnvironmentName environment={{ name: "prod", protected: true }} />);

    expect(screen.getByText("protected")).toHaveClass("env-protected");
    expect(screen.getByText("●")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("EnvironmentPicker's hidden select", () => {
  it("marks a hidden character in the options of the native select", () => {
    const { container } = render(
      <EnvironmentPicker
        environments={[{ name: "pr\u202Eod", protected: false }]}
        value={null}
        label="Show"
        all="All environments"
        onChange={vi.fn()}
      />,
    );

    const options = [...container.querySelectorAll("select option")].map(
      (option) => option.textContent,
    );
    expect(options).toContain("prU+202Eod");
    expect(options.join("")).not.toContain("\u202E");
  });
});
