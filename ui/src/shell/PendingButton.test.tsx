import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PendingButton } from "./PendingButton";

describe("PendingButton", () => {
  it("has no sign and presses while nothing is pending", async () => {
    const onPress = vi.fn();
    render(
      <PendingButton className="button" pending={false} onPress={onPress}>
        Save
      </PendingButton>,
    );
    const button = screen.getByRole("button", { name: "Save" });

    await userEvent.click(button);

    expect(button.querySelector(".pending-sign")).toBeNull();
    expect(button).not.toHaveAttribute("data-pending");
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it("shows a progress sign before the label, keeps focus and refuses a second press", async () => {
    const onPress = vi.fn();
    const user = userEvent.setup();
    render(
      <PendingButton className="button" pending onPress={onPress}>
        Save
      </PendingButton>,
    );
    const button = screen.getByRole("button", { name: /^Save/ });
    button.focus();

    await user.keyboard("{Enter}");
    await user.click(button);

    const sign = button.querySelector(".pending-sign");
    expect(sign).toHaveAttribute("role", "progressbar");
    expect(button.firstElementChild).toBe(sign);
    expect(button).toHaveAttribute("data-pending");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveFocus();
    expect(onPress).not.toHaveBeenCalled();
  });

  it("tells assistive technology that the button is busy, and still names it", () => {
    const { rerender } = render(
      <PendingButton className="button" pending={false}>
        Save
      </PendingButton>,
    );
    expect(screen.getByRole("button")).toHaveAccessibleName("Save");

    rerender(
      <PendingButton className="button" pending>
        Save
      </PendingButton>,
    );

    const button = screen.getByRole("button");
    const progress = button.querySelector(".pending-sign") as HTMLElement;
    expect(progress).not.toHaveAttribute("aria-valuenow");
    expect(button.getAttribute("aria-labelledby")?.split(" ")).toContain(progress.id);
    expect(button).toHaveAccessibleName("Save Working…");
  });

  it("keeps a label the caller gives, and adds the busy words to it", () => {
    render(
      <PendingButton className="button" pending aria-label="Withdraw the invite of kim">
        Withdraw
      </PendingButton>,
    );

    expect(screen.getByRole("button")).toHaveAccessibleName("Withdraw the invite of kim Working…");
  });

  it("styles the sign as a block that sizes and turns outside a flex button", () => {
    const css = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8");
    const rule = /\.pending-sign\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";

    expect(rule).toContain("display: inline-block");
  });
});
