import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { queryRoutes } from "../test/queries";
import { renderShell } from "../test/render";

/** Makes the window as narrow as a phone's: below 640 px. */
function narrow(): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === "(max-width: 639.98px)",
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

describe("LibraryFold", () => {
  it("below 640 px the panel is a Queries button that opens both lists", async () => {
    narrow();
    const { user, router } = renderShell("/queries", { routes: queryRoutes() });
    const button = await screen.findByRole("button", { name: "Queries" });
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Saved" })).not.toBeInTheDocument();

    await user.click(button);

    const panel = await screen.findByRole("dialog", { name: "Saved and recent queries" });
    expect(within(panel).getByRole("heading", { name: "Saved" })).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "Recent" })).toBeInTheDocument();
    await user.click(await within(panel).findByRole("link", { name: "Daily sales" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(
        "/queries/prod/saved/6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d",
      );
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(document.activeElement).not.toBe(document.body);
  });

  it("Escape returns the focus to the button", async () => {
    narrow();
    const { user } = renderShell("/queries", { routes: queryRoutes() });
    const button = await screen.findByRole("button", { name: "Queries" });
    await user.click(button);
    await screen.findByRole("dialog", { name: "Saved and recent queries" });

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(button).toHaveFocus();
    });
  });

  it("at 640 px and wider the lists stand beside the editor", async () => {
    renderShell("/queries", { routes: queryRoutes() });

    const aside = await screen.findByRole("complementary", { name: "Saved and recent queries" });
    expect(within(aside).getByRole("heading", { name: "Saved" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Queries" })).not.toBeInTheDocument();
  });
});
