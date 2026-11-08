import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

describe("AppShell", () => {
  it("has a banner with the sidebar, and one main landmark", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const banner = screen.getByRole("banner");
    expect(within(banner).getByText("Lakana")).toBeInTheDocument();
    expect(within(banner).getByRole("navigation")).toBeInTheDocument();
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it("starts with the skip link", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    expect(screen.getAllByRole("link")[0]).toHaveTextContent("Skip to content");
  });
});
