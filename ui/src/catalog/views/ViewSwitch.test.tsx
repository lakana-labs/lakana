import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { DEFAULT_VIEW } from "../view";
import { ViewSwitch } from "./ViewSwitch";

describe("ViewSwitch", () => {
  it("marks the current view with aria-current and keeps the place in every link", () => {
    render(
      <MemoryRouter>
        <ViewSwitch
          view={{ ...DEFAULT_VIEW, environment: "prod", namespace: "gold", view: "dropped" }}
        />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Views of the catalog" });
    const links = nav.querySelectorAll("a");
    expect([...links].map((link) => link.textContent)).toEqual([
      "All tables",
      "Dropped",
      "Expiring",
      "Orphans",
    ]);
    expect([...links].map((link) => link.getAttribute("href"))).toEqual([
      "/catalog?env=prod&ns=gold",
      "/catalog?env=prod&ns=gold&view=dropped",
      "/catalog?env=prod&ns=gold&view=expiring",
      "/catalog?env=prod&ns=gold&view=orphans",
    ]);
    expect(screen.getByRole("link", { name: "Dropped" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Orphans" })).not.toHaveAttribute("aria-current");
  });
});
