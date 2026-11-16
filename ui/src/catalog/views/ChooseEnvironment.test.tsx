import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { TREE } from "../../test/catalog";
import { buildTree } from "../tree";
import { DEFAULT_VIEW } from "../view";
import { ChooseEnvironment } from "./ChooseEnvironment";

describe("ChooseEnvironment", () => {
  it("lists one link per environment of the tree, keeping the view", () => {
    render(
      <MemoryRouter>
        <ChooseEnvironment view={{ ...DEFAULT_VIEW, view: "orphans" }} root={buildTree(TREE)} />
      </MemoryRouter>,
    );

    expect(screen.getByText(/are listed per environment/)).toBeInTheDocument();
    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["dev", "prod"]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/catalog?env=dev&view=orphans",
      "/catalog?env=prod&view=orphans",
    ]);
  });

  it("marks hidden characters in an environment's name", () => {
    const tree = {
      count: 0,
      environments: [{ name: "pr\u202Eod", count: 0, actions: [], namespaces: [] }],
    };
    render(
      <MemoryRouter>
        <ChooseEnvironment view={{ ...DEFAULT_VIEW, view: "orphans" }} root={buildTree(tree)} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link")).toHaveTextContent("prU+202Eod");
    expect(screen.getByText("U+202E")).toHaveClass("char-mark");
  });
});
