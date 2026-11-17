import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";
import { NotAvailable } from "./NotAvailable";

describe("NotAvailable", () => {
  it("names the area in one sentence and offers one action", async () => {
    renderShell("/settings", { access: new Set(["home"]) });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Settings" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: "Copy command" })).not.toBeInTheDocument();
  });

  it("names a place", () => {
    render(<NotAvailable place="Invites" />);

    expect(
      screen.getByRole("heading", { level: 1, name: "You cannot use Invites" }),
    ).toBeInTheDocument();
  });
});
