import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";

const SETTINGS = {
  idle_timeout_minutes: 720,
  absolute_lifetime_minutes: 10080,
  bounds: {
    idle_timeout_minutes_min: 15,
    idle_timeout_minutes_max: 1440,
    absolute_lifetime_minutes_min: 60,
    absolute_lifetime_minutes_max: 43200,
  },
};

describe("SignInSettings", () => {
  it("shows the session lengths with their bounds, edited in place", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const idle = await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    expect(idle).toHaveValue("720");
    expect(idle).toHaveAccessibleDescription(/Between 15 and 1440 minutes/);
    const absolute = screen.getByRole("textbox", { name: "Longest session in minutes" });
    expect(absolute).toHaveValue("10080");
    expect(absolute).toHaveAccessibleDescription(/Between 60 and 43200 minutes/);
    expect(screen.getByText(/applies to sessions that begin afterwards/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign-in" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("row")).not.toBeInTheDocument();
  });

  it.each([
    ["Idle timeout in minutes", "10", "The idle timeout is between 15 and 1440 minutes."],
    ["Idle timeout in minutes", "abc", "The idle timeout is between 15 and 1440 minutes."],
    ["Idle timeout in minutes", "2000", "The idle timeout is between 15 and 1440 minutes."],
    ["Longest session in minutes", "x", "The longest session is between 60 and 43200 minutes."],
    ["Longest session in minutes", "30", "The longest session is between 60 and 43200 minutes."],
    ["Longest session in minutes", "50000", "The longest session is between 60 and 43200 minutes."],
  ])("refuses an out-of-bound %s (%s) and names the bound", async (field, value, message) => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const input = await screen.findByRole("textbox", { name: field });
    await user.clear(input);
    await user.type(input, value);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("refuses a longest session shorter than the idle timeout", async () => {
    const { user } = renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const absolute = await screen.findByRole("textbox", { name: "Longest session in minutes" });
    await user.clear(absolute);
    await user.type(absolute, "600");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The longest session cannot be shorter than the idle timeout (720 minutes).",
    );
  });

  it("saves new lengths and says so", async () => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "PATCH /api/v1/settings/sign-in": (request) => ({
          status: 200,
          body: { ...SETTINGS, ...(request.body as object) },
        }),
      },
    });

    const idle = await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    await user.clear(idle);
    await user.type(idle, "60");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
    expect(server.sent.at(-1)?.body).toEqual({
      idle_timeout_minutes: 60,
      absolute_lifetime_minutes: 10080,
    });
  });

  it("shows the pending sign on Save while the lengths are saved", async () => {
    const request = held();
    const { user } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "PATCH /api/v1/settings/sign-in": request.route,
      },
    });
    await screen.findByRole("textbox", { name: "Idle timeout in minutes" });

    await user.click(screen.getByRole("button", { name: "Save" }));

    expectPending(screen.getByRole("button", { name: /^Save/ }));
    request.release({ status: 200, body: SETTINGS });
    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
  });

  it("names the server's reason when saving is refused", async () => {
    const { user } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "PATCH /api/v1/settings/sign-in": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });

  it("names the reason when the settings cannot be read", async () => {
    renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });
});
