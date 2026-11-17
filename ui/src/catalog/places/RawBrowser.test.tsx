import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LAKE, rawFile, rawFolder, rawRoutes } from "../../test/raw";
import { type FakeRoute, fakeServer } from "../../test/server";
import RawBrowser from "./RawBrowser";

/**
 * Renders the panel for `prod` over a fake lake.
 *
 * @param routes - The routes the fake server answers.
 * @returns The handlers, the server and a user-event session.
 */
function renderBrowser(routes: Record<string, FakeRoute> = rawRoutes()) {
  const server = fakeServer(routes);
  const onChoose = vi.fn();
  const user = userEvent.setup();
  render(<RawBrowser environment="prod" onChoose={onChoose} />);
  return { server, onChoose, user };
}

/**
 * The rows of the listing shown, each as the texts of its cells.
 *
 * @returns The rows.
 */
function rows(): string[][] {
  return within(screen.getByRole("region", { name: /^Folders and files in / }))
    .getAllByRole("row")
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    );
}

describe("RawBrowser", () => {
  it("lists raw/ with sizes and the folders in use, and no file of raw/ itself can be chosen", async () => {
    const { user, onChoose } = renderBrowser();

    expect(await screen.findByRole("button", { name: "feeds/" })).toBeEnabled();
    expect(rows()).toEqual([
      ["events.csv", "2 kB", ""],
      ["feeds/", "", ""],
      ["orders/", "", "in use by bronze.orders"],
      ["payments/", "", "in use"],
    ]);
    expect(screen.getByRole("button", { name: "orders/" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "payments/" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "events.csv" })).toBeDisabled();
    expect(
      screen.getByText("A table reads the files of one folder below raw/: open a folder first."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use this folder" })).not.toBeInTheDocument();
    const trail = screen.getByRole("navigation", { name: "Folder" });
    expect(within(trail).getByText("raw")).toHaveAttribute("aria-current", "location");

    await user.click(screen.getByRole("button", { name: "events.csv" }));
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("opens a folder, goes back by the trail, and a file gives its folder and ending", async () => {
    const { user, onChoose, server } = renderBrowser();

    await user.click(await screen.findByRole("button", { name: "feeds/" }));
    await screen.findByRole("button", { name: "archive/" });
    const trail = screen.getByRole("navigation", { name: "Folder" });
    expect(within(trail).getByText("feeds")).toHaveAttribute("aria-current", "location");
    expect(server.sent.at(-1)?.path).toBe(
      "/api/v1/environments/prod/raw?path=raw%2Ffeeds&limit=100",
    );

    await user.click(screen.getByRole("button", { name: "2026-11-01.CSV" }));
    expect(onChoose).toHaveBeenLastCalledWith("raw/feeds/*.CSV");

    await user.click(screen.getByRole("button", { name: "Use this folder" }));
    expect(onChoose).toHaveBeenLastCalledWith("raw/feeds/*.csv");

    await user.click(within(trail).getByRole("button", { name: "raw" }));
    expect(await screen.findByRole("button", { name: "feeds/" })).toBeInTheDocument();
  });

  it("cannot use a folder without files, and says it is empty", async () => {
    const { user } = renderBrowser();

    await user.click(await screen.findByRole("button", { name: "feeds/" }));
    await user.click(await screen.findByRole("button", { name: "archive/" }));
    expect(await screen.findByText("Nothing in raw/feeds/archive.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use this folder" })).toBeDisabled();
  });

  it("reads the next page with Show more and counts its files for the folder", async () => {
    const { user, onChoose } = renderBrowser(
      rawRoutes({ ...LAKE, raw: [[rawFolder("stations")]] }),
    );

    await user.click(await screen.findByRole("button", { name: "stations/" }));
    await screen.findByRole("button", { name: "berlin.parquet" });
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByRole("button", { name: "munich.json" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use this folder" }));
    expect(onChoose).toHaveBeenLastCalledWith("raw/stations/*.parquet");
  });

  it("shows the server's refusal with Try again", async () => {
    const detail =
      "You may not browse raw/ in prod: it needs CREATE_TABLE on one of its namespaces.";
    const { user, server } = renderBrowser({
      "GET /api/v1/environments/prod/raw": {
        status: 403,
        body: { type: "PermissionDenied", detail },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    server.route(rawRoutes());
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "feeds/" })).toBeInTheDocument();
  });

  it("shows a name's hidden characters as marks", async () => {
    renderBrowser(rawRoutes({ raw: [[rawFile("a​b.csv", 1), rawFolder("x‮y")]] }));

    expect(await screen.findByRole("button", { name: "aU+200Bb.csv" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "xU+202Ey/" })).toBeInTheDocument();
  });
});
