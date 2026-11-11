import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { SETTINGS_PLACES } from "./SettingsFrame";

describe("RepositoriesPlace", () => {
  it("shows the heading, the tabs, the intro and the way to connect", async () => {
    renderShell("/settings/repositories");

    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
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

  it.each(SETTINGS_PLACES)("has a tab for the place %s, and the place renders", async (place) => {
    renderShell(`/settings/${place}`);

    const tabs = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(tabs).getByRole("link", { current: "page" })).toHaveAttribute(
      "href",
      `/settings/${place}`,
    );
    expect(screen.queryByText("This page does not exist")).not.toBeInTheDocument();
  });

  it("has a tab for every first-level route of the Settings screen", () => {
    const source = readFileSync(resolve(import.meta.dirname, "SettingsScreen.tsx"), "utf8");
    const routes = [...source.matchAll(/<Route\s+path="([^"*]+)"/g)].map((match) => match[1]);
    const firstLevel = new Set(routes.map((path) => (path as string).split("/")[0]));

    expect(firstLevel.size).toBeGreaterThan(0);
    expect([...firstLevel].sort()).toEqual([...SETTINGS_PLACES].sort());
  });
});
