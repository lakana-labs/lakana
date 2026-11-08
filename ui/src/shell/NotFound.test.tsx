import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

describe("NotFound", () => {
  it("says what happened and offers one way back", async () => {
    renderShell("/no-such-place");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: "Copy command" })).not.toBeInTheDocument();
  });
});
