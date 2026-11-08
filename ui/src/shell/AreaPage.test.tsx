import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ALL_SHIPPED, visibleAreas } from "./areas";
import { AreaPage } from "./AreaPage";

describe("AreaPage", () => {
  it("shows the area's name, purpose and what to use until its screen comes", () => {
    const catalog = visibleAreas(ALL_SHIPPED).find((area) => area.id === "catalog");
    if (catalog === undefined) {
      throw new Error("the catalog area is missing");
    }
    render(<AreaPage area={catalog} />);

    expect(screen.getByRole("heading", { level: 1, name: "Catalog" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
    expect(screen.getByText("Find tables, their columns, health and lineage.")).toBeInTheDocument();
    expect(screen.getByText("This screen comes in part 3 of the interface.")).toBeInTheDocument();
    expect(screen.getByText("lakana search <words>")).toBeInTheDocument();
    expect(document.title).toBe("Catalog · Lakana");
  });
});
