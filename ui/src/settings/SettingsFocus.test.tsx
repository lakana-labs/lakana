import { act, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderShell } from "../test/render";

// Invites' page arrives only when the test says so, as a slow chunk would.
const slow = vi.hoisted(() => {
  let release: () => void = () => undefined;
  const ready = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    ready,
    release: () => {
      release();
    },
  };
});

vi.mock("./placePages", async (importOriginal) => {
  const original = await importOriginal<typeof import("./placePages")>();
  return {
    PLACE_PAGES: {
      ...original.PLACE_PAGES,
      invites: async () => {
        await slow.ready;
        return original.PLACE_PAGES.invites();
      },
    },
  };
});

describe("Settings' focus after a place that loads late", () => {
  it("leaves the focus where the person put it while the place loaded", async () => {
    const { user } = renderShell("/settings/status", {
      routes: { "GET /api/v1/invites": { status: 200, body: [] } },
    });
    await screen.findByRole("heading", { level: 1, name: "Status" });
    const nav = screen.getByRole("navigation", { name: "Settings places" });

    await user.click(within(nav).getByRole("link", { name: "Invites" }));
    // While the page loads, the person moves on with Tab.
    await user.tab();
    const chosen = document.activeElement;
    expect(chosen).not.toBe(document.body);
    await act(async () => {
      slow.release();
      await slow.ready;
    });

    expect(await screen.findByRole("heading", { level: 1, name: "Invites" })).not.toHaveFocus();
    expect(document.activeElement).toBe(chosen);
  });
});
