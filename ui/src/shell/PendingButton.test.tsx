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

  it("shows a hidden sign before the label, keeps focus and refuses a second press", async () => {
    const onPress = vi.fn();
    const user = userEvent.setup();
    render(
      <PendingButton className="button" pending onPress={onPress}>
        Save
      </PendingButton>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    button.focus();

    await user.keyboard("{Enter}");
    await user.click(button);

    const sign = button.querySelector(".pending-sign");
    expect(sign).toHaveAttribute("aria-hidden", "true");
    expect(button.firstElementChild).toBe(sign);
    expect(button).toHaveAttribute("data-pending");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveFocus();
    expect(onPress).not.toHaveBeenCalled();
  });
});
