import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { HeldGrantOut } from "../api/grants";
import { HeldGrants } from "./HeldGrants";

const DIRECT: HeldGrantOut = {
  id: "g-1",
  role: null,
  action: "READ_TABLE",
  kind: "ENVIRONMENT",
  environment: "prod",
  namespace: null,
  table: null,
  granted_at: "2026-11-01T09:00:00Z",
  note: null,
  group: null,
};

const THROUGH: HeldGrantOut = {
  ...DIRECT,
  id: "g-2",
  role: "ANALYST",
  action: null,
  environment: "dev",
  group: "analysts",
};

describe("HeldGrants", () => {
  it("says through which group a grant comes", () => {
    render(
      <MemoryRouter>
        <HeldGrants grants={[DIRECT, THROUGH]} label="Your grants" linkGroups />
      </MemoryRouter>,
    );

    const [, direct, through] = screen.getAllByRole("row");
    expect(direct).toHaveTextContent("Read tables");
    expect(within(direct as HTMLElement).getByText("directly")).toBeInTheDocument();
    expect(through).toHaveTextContent("ANALYSTRead tables and run queries");
    expect(within(through as HTMLElement).getByRole("link", { name: "analysts" })).toHaveAttribute(
      "href",
      "/settings/access/groups/analysts",
    );
  });

  it("shows the note of an action without effect, and a group without a link", () => {
    render(
      <MemoryRouter>
        <HeldGrants
          grants={[{ ...THROUGH, role: null, action: "MANAGE_GRANT", note: "No effect." }]}
          label="Grants"
          linkGroups={false}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("No effect.")).toHaveClass("second");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("analysts")).toBeInTheDocument();
  });

  it("marks a hidden character in a group's name", () => {
    const { container } = render(
      <MemoryRouter>
        <HeldGrants grants={[{ ...THROUGH, group: "an‮al" }]} label="x" linkGroups />
      </MemoryRouter>,
    );

    expect(container).toHaveTextContent("anU+202Eal");
    expect(container.textContent).not.toContain("‮");
  });

  it("scrolls in its own named region", () => {
    render(
      <MemoryRouter>
        <HeldGrants grants={[DIRECT]} label="Your grants" linkGroups />
      </MemoryRouter>,
    );

    expect(screen.getByRole("region", { name: "Your grants" })).toHaveAttribute("tabindex", "0");
  });
});
