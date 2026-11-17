import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { SETTINGS_PLACES } from "./places";

describe("RepositoriesPlace", () => {
  it("shows the heading, the list of places, the intro and the way to connect", async () => {
    renderShell("/settings/repositories");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Repositories" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Status" })).toHaveAttribute(
      "href",
      "/settings/status",
    );
    expect(screen.getByRole("link", { name: "Sign-in" })).toHaveAttribute(
      "href",
      "/settings/sign-in",
    );
    expect(screen.getByText(/only ever reads from them/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect a repository" })).toHaveAttribute(
      "href",
      "/settings/repositories/new",
    );
  });

  it("leads to Connect a repository, whose breadcrumb names Repositories", async () => {
    const { user } = renderShell("/settings/repositories");

    await user.click(await screen.findByRole("link", { name: "Connect a repository" }));

    const crumbs = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "href",
      "/settings/repositories",
    );
  });

  it.each(SETTINGS_PLACES.map((place) => place.path))(
    "lists the place %s, marks it current, and the place renders",
    async (path) => {
      renderShell(`/settings/${path}`);

      const places = await screen.findByRole("navigation", { name: "Settings places" });
      expect(within(places).getByRole("link", { current: "page" })).toHaveAttribute(
        "href",
        `/settings/${path}`,
      );
      expect(screen.queryByText("This page does not exist")).not.toBeInTheDocument();
    },
  );
});
