import { act, screen, within } from "@testing-library/react";
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

  it("keeps focus when only the query changes", async () => {
    const { router } = renderShell("/queries");
    await screen.findByRole("heading", { level: 1, name: "Queries" });
    const link = screen.getByRole("link", { name: "Jobs" });
    link.focus();

    await act(() => router.navigate("/queries?sort=rows"));

    expect(link).toHaveFocus();
  });

  it("still focuses the heading after moving to another page", async () => {
    const { router } = renderShell("/queries");
    await screen.findByRole("heading", { level: 1, name: "Queries" });

    await act(() => router.navigate("/jobs"));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
  });
});
