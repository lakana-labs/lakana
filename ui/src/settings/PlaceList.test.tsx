import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { PlaceList } from "./PlaceList";
import type { SettingsPlace } from "./places";

const ADMIN = { admin: true, noAccessYet: false };

/**
 * A page place for the synthetic registry below.
 *
 * @param id - Its id, path segment and label.
 * @param parent - The place above it, or null.
 * @returns The place.
 */
const page = (id: string, parent: string | null): SettingsPlace =>
  ({
    id,
    path: parent === null ? id : `${parent}/${id}`,
    label: id,
    parent,
    kind: "page",
    below: false,
    visible: () => true,
  }) as SettingsPlace;

const TREE: readonly SettingsPlace[] = [
  page("top", null),
  page("kid", "top"),
  { ...page("grp", null), kind: "group" },
  page("inside", "grp"),
];

describe("PlaceList", () => {
  it("names a group without a link and indents each place's children beneath it", () => {
    render(
      <MemoryRouter initialEntries={["/settings/grp/inside"]}>
        <PlaceList viewer={ADMIN} places={TREE} />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Settings places" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["top", "kid", "inside"]);
    expect(within(nav).queryByRole("link", { name: "grp" })).not.toBeInTheDocument();
    const group = within(nav).getByText("grp").closest("li");
    expect(group).not.toBeNull();
    expect(within(group as HTMLElement).getByRole("link", { name: "inside" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const top = within(nav).getByRole("link", { name: "top" }).closest("li");
    expect(within(top as HTMLElement).getByRole("link", { name: "kid" })).toHaveAttribute(
      "href",
      "/settings/top/kid",
    );
  });
});
