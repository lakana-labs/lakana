import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

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

describe("PlaceFold", () => {
  it("a narrow window folds the list into Section", async () => {
    narrow();
    const { user, router } = renderShell("/settings/status");
    const button = await screen.findByRole("button", { name: "Section" });
    expect(screen.queryByRole("navigation", { name: "Settings places" })).not.toBeInTheDocument();

    await user.click(button);

    const dialog = await screen.findByRole("dialog", { name: "Settings places" });
    expect(
      within(dialog)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual([
      "Status",
      "Repositories",
      "People",
      "Groups",
      "Grants",
      "Tokens",
      "Secrets",
      "Row filters & masks",
      "Access file",
      "What I may do",
      "Sign-in",
      "Invites",
      "Recovery links",
    ]);
    await user.click(within(dialog).getByRole("link", { name: "Sign-in" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings/sign-in");
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    // The Sign-in page loads on demand: reopen Section only once it has shown and taken the focus.
    expect(await screen.findByRole("heading", { level: 1, name: "Sign-in" })).toBeInTheDocument();

    const again = screen.getByRole("button", { name: "Section" });
    await user.click(again);
    await screen.findByRole("dialog", { name: "Settings places" });
    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(again).toHaveFocus();
    });
  });

  it("choosing the place already shown closes Section too", async () => {
    narrow();
    const { user, router } = renderShell("/settings/status");
    await user.click(await screen.findByRole("button", { name: "Section" }));
    const dialog = await screen.findByRole("dialog", { name: "Settings places" });

    await user.click(within(dialog).getByRole("link", { name: "Status" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(router.state.location.pathname).toBe("/settings/status");
  });

  it("a wide window lists the places beside the place, with no Section", async () => {
    renderShell("/settings/status");

    expect(await screen.findByRole("navigation", { name: "Settings places" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Section" })).not.toBeInTheDocument();
  });
});
