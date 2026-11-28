import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button, Heading } from "react-aria-components";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "../i18n/LanguageProvider";
import { ActionDialog } from "./ActionDialog";

function renderDialog(onClose: () => void): ReturnType<typeof userEvent.setup> {
  const user = userEvent.setup();
  render(
    <LanguageProvider>
      <ActionDialog onClose={onClose}>
        <Heading slot="title">Delete gold.station_ranking</Heading>
        <Button className="button" onPress={onClose}>
          Cancel
        </Button>
      </ActionDialog>
    </LanguageProvider>,
  );
  return user;
}

describe("ActionDialog", () => {
  it("has a close button named for screen readers that does what Cancel does", async () => {
    const onClose = vi.fn();
    const user = renderDialog(onClose);
    const dialog = screen.getByRole("dialog", { name: "Delete gold.station_ranking" });

    const close = within(dialog).getByRole("button", { name: "Close dialog" });
    expect(close).toHaveClass("dialog-close");
    await user.click(close);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("scrolls its content in a body of its own, so the × stays in its corner", () => {
    renderDialog(vi.fn());
    const dialog = screen.getByRole("dialog", { name: "Delete gold.station_ranking" });

    const body = dialog.querySelector(".dialog-body");
    expect(body).toContainElement(
      screen.getByRole("heading", { name: "Delete gold.station_ranking" }),
    );
    expect(body).not.toContainElement(screen.getByRole("button", { name: "Close dialog" }));
    expect(screen.getByRole("heading")).toHaveAttribute("slot", "title");
  });

  it("reaches the close button by keyboard and presses it with Enter", async () => {
    const onClose = vi.fn();
    const user = renderDialog(onClose);

    await user.tab();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close dialog" })).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("still closes with Escape", async () => {
    const onClose = vi.fn();
    const user = renderDialog(onClose);

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
