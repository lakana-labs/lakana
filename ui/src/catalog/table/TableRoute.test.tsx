import { screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { held } from "../../test/pending";
import { renderShell } from "../../test/render";
import { table, TABLE_PATH } from "../../test/table";

// The page's chunk arrives after the second the loading line waits for, as on a slow line.
vi.mock("./TablePage", async (original) => {
  await new Promise((resolve) => setTimeout(resolve, 1500));
  return original();
});

it("says the table is loading once, in one status that stays through the chunk and the read", async () => {
  const slow = held();
  const { server } = renderShell("/catalog/prod/gold.sales/fact_sales", {
    routes: { [`GET ${TABLE_PATH}`]: slow.route },
  });

  const status = await screen.findByRole("status", { name: "" }, { timeout: 2500 });
  expect(status).toHaveTextContent("Loading the table…");
  await waitFor(
    () => {
      expect(server.sent.some((request) => request.path === TABLE_PATH)).toBe(true);
    },
    { timeout: 3000 },
  );

  expect(status.isConnected).toBe(true);
  expect(screen.getAllByText("Loading the table…")).toHaveLength(1);
  slow.release({ status: 200, body: table() });
  expect(await screen.findByRole("navigation", { name: "Sections of the table" })).toBeVisible();
  await waitFor(() => {
    expect(screen.queryByText("Loading the table…")).not.toBeInTheDocument();
  });
}, 10000);
