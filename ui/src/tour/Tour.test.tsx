import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AccessOut, InterfaceStateOut } from "../api";
import { areasFor } from "../shell/areas";
import { STATE } from "../test/home";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const STATE_PATH = "/api/v1/principals/admin/interface-state";

/** The steps of a fresh administrator's Get started list: a repository first. */
const FRESH_ADMIN = {
  audience: "admin",
  local: false,
  steps: [
    { id: "connect_repository", done: false },
    { id: "invite_team", done: false },
  ],
  hidden: false,
  complete: false,
};

/** Options of {@link firstSignIn}. */
interface FirstSignIn {
  /** The person's stored state beyond `tour_seen: false`. */
  readonly state?: Partial<InterfaceStateOut>;
  /** The version the server runs. */
  readonly version?: string;
  /** What the person may do. */
  readonly me?: Partial<AccessOut>;
  /** Server routes beyond the default. */
  readonly routes?: Record<string, FakeRoute>;
}

/**
 * Renders the shell at Home for a person who never saw the tour; PATCH answers what was sent.
 *
 * @param options - The state, the version, the person and more routes.
 * @returns The shell.
 */
function firstSignIn(options: FirstSignIn = {}): ReturnType<typeof renderShell> {
  const name = options.me?.name ?? "admin";
  const path = `/api/v1/principals/${name}/interface-state`;
  let stored: InterfaceStateOut = { ...STATE, tour_seen: false, ...options.state };
  return renderShell("/", {
    me: options.me ?? {},
    access: areasFor(options.me?.admin !== false, false),
    routes: {
      [`GET ${path}`]: { status: 200, body: stored },
      [`PATCH ${path}`]: (request) => {
        stored = { ...stored, ...(request.body as object) };
        return { status: 200, body: stored };
      },
      "GET /api/v1/info": { status: 200, body: { version: options.version ?? "0.23.0" } },
      "GET /api/v1/home/get-started": { status: 200, body: FRESH_ADMIN },
      ...options.routes,
    },
  });
}

/**
 * The PATCH bodies the interface sent for the person's state.
 *
 * @param server - The fake server.
 * @param path - The state's path.
 * @returns The bodies, in order.
 */
function patches(server: ReturnType<typeof renderShell>["server"], path = STATE_PATH): unknown[] {
  return server.sent
    .filter((request) => request.method === "PATCH" && request.path === path)
    .map((request) => request.body);
}

/**
 * Waits for the welcome and starts the tour.
 *
 * @param user - The user-event session.
 * @returns The first step's dialog.
 */
async function startTour(user: ReturnType<typeof renderShell>["user"]): Promise<HTMLElement> {
  const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });
  await user.click(within(welcome).getByRole("button", { name: "Start the tour" }));
  return screen.findByRole("dialog", { name: "Home" });
}

/**
 * Reads the current step's name.
 *
 * @returns The name of the one dialog on screen.
 */
function currentStep(): string {
  const dialogs = screen.getAllByRole("dialog");
  expect(dialogs).toHaveLength(1);
  return dialogs[0]?.getAttribute("aria-labelledby") === null
    ? ""
    : (document.getElementById(dialogs[0]?.getAttribute("aria-labelledby") ?? "")?.textContent ??
        "");
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the tour", () => {
  it("welcomes a person whose tour_seen is false, once", async () => {
    const { user, server } = firstSignIn();

    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });
    expect(welcome).toHaveTextContent(/Lakana keeps/);
    expect(within(welcome).getByRole("button", { name: "Skip" })).toBeInTheDocument();
    expect(screen.getAllByRole("dialog", { name: "Welcome to Lakana" })).toHaveLength(1);

    await user.click(within(welcome).getByRole("button", { name: "Skip" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(patches(server)).toContainEqual({ tour_seen: true });
    });
    // Back at Home again, nothing welcomes the person a second time.
    await user.click(screen.getByRole("link", { name: "Catalog" }));
    await user.click(screen.getByRole("link", { name: "Home" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not welcome a person who saw the tour", async () => {
    const { server } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await waitFor(() => {
      expect(server.sent.map((request) => request.path)).toContain(STATE_PATH);
    });
    await act(() => Promise.resolve());

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("waits with the welcome until a person has access", async () => {
    const { server } = firstSignIn({ me: { name: "kim", admin: false, noAccessYet: true } });
    await screen.findByText(/You have no access yet/);
    await waitFor(() => {
      expect(server.sent.map((request) => request.path)).toContain(
        "/api/v1/principals/kim/interface-state",
      );
    });
    await act(() => Promise.resolve());

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("moves focus into the welcome and into each step", async () => {
    const { user } = firstSignIn();

    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });
    await waitFor(() => {
      expect(welcome.contains(document.activeElement)).toBe(true);
    });
    const step = await startTour(user);
    await waitFor(() => {
      expect(step.contains(document.activeElement)).toBe(true);
    });
  });

  it("never advances on its own", async () => {
    const { user } = firstSignIn();
    await startTour(user);

    vi.useFakeTimers();
    act(() => {
      vi.advanceTimersByTime(10 * 60 * 1000);
    });
    vi.useRealTimers();
    // Only Escape leaves; another key does nothing.
    await user.keyboard("a");

    expect(currentStep()).toBe("Home");
  });

  it("has Back, Next and Skip tour on every step", async () => {
    const { user } = firstSignIn();
    let step = await startTour(user);
    expect(within(step).queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
    expect(step).toHaveTextContent("Step 1 of 9");

    const seen: string[] = [];
    for (let index = 0; index < 8; index += 1) {
      seen.push(currentStep());
      expect(within(step).getByRole("button", { name: "Skip tour" })).toBeInTheDocument();
      if (index > 0) {
        expect(within(step).getByRole("button", { name: "Back" })).toBeInTheDocument();
      }
      await user.click(within(step).getByRole("button", { name: "Next" }));
      step = await screen.findByRole("dialog", { name: /./ });
    }
    expect(currentStep()).toBe("Help");
    expect(within(step).getByRole("button", { name: "Back" })).toBeInTheDocument();
    expect(within(step).getByRole("button", { name: "Finish" })).toBeInTheDocument();
    expect(seen).toEqual([
      "Home",
      "Catalog",
      "Queries",
      "Jobs",
      "Connections",
      "Access",
      "Settings",
      "Search",
    ]);

    await user.click(within(step).getByRole("button", { name: "Back" }));
    expect(currentStep()).toBe("Search");
  });

  it("outlines the place each step is about", async () => {
    const { user } = firstSignIn();
    const step = await startTour(user);
    expect(screen.getByRole("link", { name: "Home" })).toHaveClass("tour-target");

    await user.click(within(step).getByRole("button", { name: "Next" }));
    await screen.findByRole("dialog", { name: "Catalog" });
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveClass("tour-target");
    expect(screen.getByRole("link", { name: "Catalog" })).toHaveClass("tour-target");
  });

  it("follows the sidebar and leaves out areas the person cannot use", async () => {
    const { user } = firstSignIn({
      me: { name: "kim", admin: false, mayDeploy: false },
      routes: {
        "GET /api/v1/home/get-started": {
          status: 200,
          body: { ...FRESH_ADMIN, audience: "reader", steps: [] },
        },
      },
    });
    let step = await startTour(user);

    const seen: string[] = [];
    for (;;) {
      seen.push(currentStep());
      const next = within(step).queryByRole("button", { name: "Next" });
      if (next === null) {
        break;
      }
      await user.click(next);
      step = await screen.findByRole("dialog", { name: /./ });
    }
    expect(seen).toEqual(["Home", "Catalog", "Queries", "Jobs", "Connections", "Search", "Help"]);
    expect(step).toHaveTextContent("Step 7 of 7");
  });

  it.each([
    ["Skip tour", false],
    ["Escape", false],
    ["Finish", true],
  ] as const)("records the tour as seen on %s", async (how, toTheEnd) => {
    const { user, server } = firstSignIn();
    let step = await startTour(user);
    if (toTheEnd) {
      for (let index = 0; index < 8; index += 1) {
        await user.click(within(step).getByRole("button", { name: "Next" }));
        step = await screen.findByRole("dialog", { name: /./ });
      }
    }

    if (how === "Escape") {
      await user.keyboard("{Escape}");
    } else {
      await user.click(within(step).getByRole("button", { name: how }));
    }

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(patches(server)[0]).toEqual({ tour_seen: true });
    });
  });

  it.each([
    ["Skip tour", false],
    ["Escape", false],
    ["Finish", true],
  ] as const)(
    "after a tour begun at the welcome, %s returns focus to the page's heading",
    async (how, toTheEnd) => {
      const { user } = firstSignIn();
      let step = await startTour(user);
      if (toTheEnd) {
        for (let index = 0; index < 8; index += 1) {
          await user.click(within(step).getByRole("button", { name: "Next" }));
          step = await screen.findByRole("dialog", { name: /./ });
        }
      }

      if (how === "Escape") {
        await user.keyboard("{Escape}");
      } else {
        await user.click(within(step).getByRole("button", { name: how }));
      }

      await waitFor(() => {
        expect(screen.getByRole("heading", { level: 1, name: "Home" })).toHaveFocus();
      });
    },
  );

  it("leaves an Escape that something else handled alone", async () => {
    const { user } = firstSignIn();
    await startTour(user);
    // A native field outside React Aria that uses Escape itself (clears its text).
    const field = document.createElement("input");
    field.setAttribute("aria-label", "A field that clears on Escape");
    field.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
      }
    });
    document.body.append(field);
    try {
      // Dispatched on the field, it bubbles to the document as a key pressed there would.
      expect(fireEvent.keyDown(field, { key: "Escape" })).toBe(false);

      expect(currentStep()).toBe("Home");
    } finally {
      field.remove();
    }
  });

  it("records the tour when Escape closes the welcome", async () => {
    const { user, server } = firstSignIn();
    await screen.findByRole("dialog", { name: "Welcome to Lakana" });

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(patches(server)[0]).toEqual({ tour_seen: true });
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sets update_note_seen to the running version when none was seen", async () => {
    const { user, server } = firstSignIn({ version: "0.23.0" });
    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });

    await user.click(within(welcome).getByRole("button", { name: "Skip" }));

    // Each in a request of its own: the server refuses the whole request for a bad version.
    await waitFor(() => {
      expect(patches(server)).toEqual([{ tour_seen: true }, { update_note_seen: "0.23.0" }]);
    });
  });

  it("keeps an update note version already seen", async () => {
    const { user, server } = firstSignIn({ state: { update_note_seen: "0.22.0" } });
    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });

    await user.click(within(welcome).getByRole("button", { name: "Skip" }));

    await waitFor(() => {
      expect(patches(server)).toEqual([{ tour_seen: true }]);
    });
  });

  it("records the tour on a development build without an update note version", async () => {
    const { user, server } = firstSignIn({ version: "0.23.1.dev4+g8dc908d7" });
    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });

    await user.click(within(welcome).getByRole("button", { name: "Skip" }));

    await waitFor(() => {
      expect(patches(server)).toEqual([{ tour_seen: true }]);
    });
  });

  it("still closes when recording the tour fails", async () => {
    const { user } = firstSignIn({
      routes: { [`PATCH ${STATE_PATH}`]: { status: 503, body: { detail: "down" } } },
    });
    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });

    await user.click(within(welcome).getByRole("button", { name: "Skip" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("names the first open Get started step on the last step", async () => {
    const { user, router } = firstSignIn();
    let step = await startTour(user);
    for (let index = 0; index < 8; index += 1) {
      await user.click(within(step).getByRole("button", { name: "Next" }));
      step = await screen.findByRole("dialog", { name: /./ });
    }

    const next = await within(step).findByRole("link", { name: "Connect your first repository" });
    expect(next).toHaveAttribute("href", "/settings/repositories/new");
    for (const [name, path] of [
      ["The getting-started tutorial", "tutorials/getting-started/"],
      ["How-to guides", "how-to/"],
      ["The documentation overview", ""],
    ] as const) {
      // jsdom drops the space before the hidden "(opens in a new tab)"; browsers keep it.
      const link = within(step).getByRole("link", { name: new RegExp(`^${name}.*new tab`) });
      expect(link).toHaveAttribute("href", `https://lakana-labs.github.io/lakana/${path}`);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noreferrer");
    }

    await user.click(next);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings/repositories/new");
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("names a step that opens the documentation site as a link there", async () => {
    const { user } = firstSignIn({
      routes: {
        "GET /api/v1/home/get-started": {
          status: 200,
          body: { ...FRESH_ADMIN, steps: [{ id: "ci_deploy", done: false }] },
        },
      },
    });
    let step = await startTour(user);
    for (let index = 0; index < 8; index += 1) {
      await user.click(within(step).getByRole("button", { name: "Next" }));
      step = await screen.findByRole("dialog", { name: /./ });
    }

    expect(
      await within(step).findByRole("link", {
        name: /^Deploy from GitHub Actions.*new tab/,
      }),
    ).toHaveAttribute(
      "href",
      "https://lakana-labs.github.io/lakana/how-to/deploy-from-github-actions/",
    );
  });

  it("names no next step when Get started is complete or cannot be read", async () => {
    const { user } = firstSignIn({
      routes: { "GET /api/v1/home/get-started": { status: 503, body: { detail: "down" } } },
    });
    let step = await startTour(user);
    for (let index = 0; index < 8; index += 1) {
      await user.click(within(step).getByRole("button", { name: "Next" }));
      step = await screen.findByRole("dialog", { name: /./ });
    }

    expect(within(step).queryByText(/Your next step/)).not.toBeInTheDocument();
    expect(within(step).getAllByRole("link")).toHaveLength(3);
  });

  it("centres every step when the sidebar is folded", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 639.98px)",
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const { user } = firstSignIn();
    const welcome = await screen.findByRole("dialog", { name: "Welcome to Lakana" });
    await user.click(within(welcome).getByRole("button", { name: "Start the tour" }));

    const step = await screen.findByRole("dialog", { name: "In the menu: Home" });
    expect(step.closest(".tour-modal")).not.toBeNull();
    await user.click(within(step).getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("dialog", { name: "In the menu: Catalog" })).toBeInTheDocument();
  });

  it("follows the window when it narrows", async () => {
    let listener: ((event: { matches: boolean }) => void) | undefined;
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: false,
        media: query,
        addEventListener: (_type: string, handler: (event: { matches: boolean }) => void) => {
          listener = handler;
        },
        removeEventListener: vi.fn(),
      })),
    );
    const { user } = firstSignIn();
    await startTour(user);

    act(() => {
      listener?.({ matches: true });
    });

    expect(await screen.findByRole("dialog", { name: "In the menu: Home" })).toBeInTheDocument();
  });

  it("restarts from Help and returns focus there", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    const help = screen.getByRole("button", { name: "Help" });

    await user.click(help);
    await user.click(await screen.findByRole("menuitem", { name: "Take the tour again" }));

    const step = await screen.findByRole("dialog", { name: "Home" });
    await user.click(within(step).getByRole("button", { name: "Skip tour" }));
    await waitFor(() => {
      expect(help).toHaveFocus();
    });
  });

  it("restarts from the command palette, and returns focus to the page's heading", async () => {
    const { user, server } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    await user.keyboard("{Control>}k{/Control}");
    await user.keyboard("tour");
    await user.keyboard("{ArrowDown}{Enter}");

    const step = await screen.findByRole("dialog", { name: "Home" });
    expect(step).toHaveTextContent("Step 1 of 9");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    // The palette's field is gone, so focus goes to the page's heading.
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Home" })).toHaveFocus();
    });
    // A person who saw the tour sends no tour_seen again; a missing update note version, yes.
    await waitFor(() => {
      expect(patches(server)).toEqual([{ update_note_seen: "0.23.0" }]);
    });
  });
});
