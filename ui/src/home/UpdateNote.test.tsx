import { cleanup, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { STATE } from "../test/home";
import { renderShell } from "../test/render";

/**
 * Renders Home for a person who last saw one version's note, on a server of another.
 *
 * @param seen - The version whose note the person saw, or null.
 * @param running - The version the server runs.
 * @returns The rendered shell.
 */
function home(seen: string | null, running: string): ReturnType<typeof renderShell> {
  let state = { ...STATE, update_note_seen: seen };
  return renderShell("/", {
    routes: {
      "GET /api/v1/info": { status: 200, body: { version: running } },
      "GET /api/v1/principals/admin/interface-state": () => ({ status: 200, body: state }),
      "PATCH /api/v1/principals/admin/interface-state": (request) => {
        state = { ...state, ...(request.body as object) };
        return { status: 200, body: state };
      },
    },
  });
}

describe("UpdateNote", () => {
  it("shows no update note before the person has seen any version", async () => {
    home(null, "0.23.0");

    await screen.findByText("Healthy");
    expect(screen.queryByText(/Lakana was updated/)).not.toBeInTheDocument();
  });

  it("shows the update note once per version until hidden", async () => {
    const { user, server } = home("0.22.0", "0.23.0");

    expect(await screen.findByText(/Lakana was updated to 0\.23\.0/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Hide the update note" }));

    expect(screen.queryByText(/Lakana was updated/)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toHaveFocus();
    const patches = server.sent.filter((request) => request.method === "PATCH");
    expect(patches.map((request) => request.body)).toEqual([{ update_note_seen: "0.23.0" }]);
    cleanup();

    home("0.23.0", "0.23.0");
    await screen.findByText("Healthy");
    expect(screen.queryByText(/Lakana was updated/)).not.toBeInTheDocument();
  });

  it("links the note to the release of that version", async () => {
    home("0.22.0", "0.23.0");

    const link = await screen.findByRole("link", { name: /^what's new/ });
    expect(link.getAttribute("href")).toMatch(/\/releases\/tag\/v0\.23\.0$/);
  });

  it("shows no note on a development build", async () => {
    home("0.22.0", "0.23.1.dev4+g8dc908d7");

    await screen.findByText("Healthy");
    expect(screen.queryByText(/Lakana was updated/)).not.toBeInTheDocument();
  });

  it("shows no note while the version is unknown", async () => {
    renderShell("/", {
      routes: {
        "GET /api/v1/info": { status: 503, body: { detail: "Busy.", type: "Busy" } },
        "GET /api/v1/principals/admin/interface-state": {
          status: 200,
          body: { ...STATE, update_note_seen: "0.22.0" },
        },
      },
    });

    await screen.findByText("Healthy");
    expect(screen.queryByText(/Lakana was updated/)).not.toBeInTheDocument();
  });

  it("says why hiding the note did not work", async () => {
    const { user } = renderShell("/", {
      routes: {
        "GET /api/v1/principals/admin/interface-state": {
          status: 200,
          body: { ...STATE, update_note_seen: "0.22.0" },
        },
        "PATCH /api/v1/principals/admin/interface-state": {
          status: 503,
          body: { detail: "The database is busy. Try again in a minute.", type: "Busy" },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Hide the update note" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The database is busy.");
    expect(screen.getByText(/Lakana was updated/)).toBeVisible();
  });
});
