import { act, cleanup, screen, within } from "@testing-library/react";
import { type JSX, useEffect } from "react";
import { beforeAll, describe, expect, it } from "vitest";

import { preloadJobsScreen } from "../test/jobs";
import { renderShell } from "../test/render";
import { Page } from "./Page";

describe("AppShell", () => {
  beforeAll(preloadJobsScreen);

  it("has a banner with the sidebar, and one main landmark", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const banner = screen.getByRole("banner");
    expect(within(banner).getByText("Lakana")).toBeInTheDocument();
    expect(within(banner).getByRole("navigation")).toBeInTheDocument();
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it("starts with the skip link", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    expect(screen.getAllByRole("link")[0]).toHaveTextContent("Skip to content");
  });

  it("keeps focus when only the query changes", async () => {
    const { router } = renderShell("/queries");
    await screen.findByRole("heading", { level: 1, name: "Queries" });
    const link = screen.getByRole("link", { name: "Jobs" });
    link.focus();

    await act(() => router.navigate("/queries?sort=rows"));

    expect(link).toHaveFocus();
  });

  it("keeps focus after a move to another page that asks to keep it", async () => {
    const { router } = renderShell("/catalog");
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    const link = screen.getByRole("link", { name: "Jobs" });
    link.focus();

    await act(() => router.navigate("/catalog/prod", { state: { keepFocus: true } }));

    expect(link).toHaveFocus();
  });

  it("focuses the heading on Back to an entry that kept the focus when it was pushed", async () => {
    const { router } = renderShell("/catalog");
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    await act(() => router.navigate("/jobs", { state: { keepFocus: true } }));
    await screen.findByRole("heading", { level: 1, name: "Jobs" });
    await act(() => router.navigate("/connections"));
    await screen.findByRole("heading", { level: 1, name: "Connections" });
    screen.getByRole("link", { name: "Home" }).focus();

    await act(() => router.navigate(-1));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
  });

  it("still focuses the heading after moving to another page", async () => {
    const { router } = renderShell("/queries");
    await screen.findByRole("heading", { level: 1, name: "Queries" });

    await act(() => router.navigate("/jobs"));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
  });

  it("leaves the focus alone when the first load is redirected", async () => {
    // Status is opened once first, so that its chunk is cached: the redirect's focus would
    // otherwise find no heading yet, and the test would pass without the guard.
    const first = renderShell("/settings/status");
    await screen.findByRole("heading", { level: 1, name: "Status" });
    cleanup();
    first.router.dispose();

    renderShell("/settings");
    const heading = await screen.findByRole("heading", { level: 1, name: "Status" });
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });

    expect(heading).not.toHaveFocus();
  });

  it("focuses the heading after a replacing move the person made on the first page", async () => {
    // A page opened directly, then a person's action that replaces its entry (a list after a
    // delete): only a redirect is part of the page opening, not every Replace.
    const { router } = renderShell("/catalog");
    await screen.findByRole("heading", { level: 1, name: "Catalog" });

    await act(() => router.navigate("/jobs", { replace: true }));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
  });

  it("focuses the heading when a move to a redirecting address lands on its place", async () => {
    const { router } = renderShell("/jobs");
    await screen.findByRole("heading", { level: 1, name: "Jobs" });

    await act(() => router.navigate("/settings"));

    expect(await screen.findByRole("heading", { level: 1, name: "Status" })).toHaveFocus();
  });

  it("focuses the heading again when returning to the first page", async () => {
    const { router } = renderShell("/catalog");
    await screen.findByRole("heading", { level: 1, name: "Catalog" });

    await act(() => router.navigate("/"));
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toHaveFocus();
    await act(() => router.navigate("/catalog"));

    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" })).toHaveFocus();
  });

  it("has moved focus to the heading before the new page's own effects run", async () => {
    // What a page's effect sees on arrival; it can then take focus itself (a note on what just
    // happened) without waiting a frame for the shell's move.
    const seen: (Element | null)[] = [];
    function Probe(): JSX.Element {
      useEffect(() => {
        seen.push(document.activeElement);
      }, []);
      return <p>Probe</p>;
    }
    const { router } = renderShell("/", {
      pages: {
        jobs: () =>
          Promise.resolve({
            AreaScreen: () => (
              <Page title="Probed">
                <Probe />
              </Page>
            ),
          }),
      },
    });
    await screen.findByRole("heading", { level: 1, name: "Home" });

    await act(() => router.navigate("/jobs"));

    await screen.findByText("Probe");
    expect(seen).toEqual([screen.getByRole("heading", { level: 1, name: "Probed" })]);
  });

  it("goes back with Alt+Left, as the browser does, so a breadcrumb's page is left by keyboard", async () => {
    const { router, user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await act(() => router.navigate("/catalog"));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });

    await user.keyboard("{Alt>}{ArrowLeft}{/Alt}");

    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("leaves Alt+Left to a text field, where it moves the cursor, and to a widget that used it", async () => {
    const { router, user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await act(() => router.navigate("/catalog"));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    const field = document.createElement("input");
    document.body.append(field);
    field.focus();

    await user.keyboard("{Alt>}{ArrowLeft}{/Alt}");
    field.remove();
    const used = (event: KeyboardEvent): void => {
      event.preventDefault();
    };
    document.body.addEventListener("keydown", used);
    await user.keyboard("{Alt>}{ArrowLeft}{/Alt}");
    document.body.removeEventListener("keydown", used);
    // With another modifier it is not the shortcut either.
    await user.keyboard("{Alt>}{Shift>}{ArrowLeft}{/Shift}{/Alt}");

    expect(router.state.location.pathname).toBe("/catalog");
  });
});
