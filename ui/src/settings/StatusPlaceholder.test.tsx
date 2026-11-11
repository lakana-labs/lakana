import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

describe("StatusPlaceholder", () => {
  it("redirects Settings to its Status placeholder", async () => {
    const { router } = renderShell("/settings");

    expect(await screen.findByText("lakana status")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/status");
    expect(screen.getByText(/part 8 of the interface/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Status" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Sign-in" })).toHaveAttribute(
      "href",
      "/settings/sign-in",
    );
  });
});
