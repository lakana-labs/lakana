import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectPending, held } from "../test/pending";
import { fakeServer, type FakeRoute } from "../test/server";
import { PictureDialog } from "./PictureDialog";

const PICTURE = "/api/v1/principals/kim.lee/picture";
const WEBP = { status: 200, blob: new Blob(["webp"], { type: "image/webp" }) };

/**
 * Renders the dialog for kim.lee against a fake server.
 *
 * @param etag - The picture kim.lee has, or null.
 * @param routes - The server's routes beyond reading the picture.
 * @returns The user, the server and the close handler.
 */
function open(etag: string | null, routes: Record<string, FakeRoute> = {}) {
  const server = fakeServer({ [`GET ${PICTURE}`]: WEBP, ...routes });
  const onClose = vi.fn();
  // Someone may pick "All files" in the browser's chooser: `accept` is only a hint.
  const user = userEvent.setup({ applyAccept: false });
  render(<PictureDialog name="kim.lee" displayName="Kim Lee" etag={etag} onClose={onClose} />);
  return { user, server, onClose };
}

/**
 * The uploads the server received.
 *
 * @param server - The fake server.
 * @param server.sent - What it received.
 * @returns The PUT requests.
 */
function uploads(server: { sent: { method: string }[] }): unknown[] {
  return server.sent.filter((request) => request.method === "PUT");
}

describe("PictureDialog", () => {
  it("says what it accepts and that the server removes a photo's details", () => {
    open(null);

    expect(screen.getByRole("heading", { name: "Profile picture" })).toBeInTheDocument();
    expect(
      screen.getByText(/JPEG, PNG or WebP image of up to 5 MB and 40 megapixels/),
    ).toBeInTheDocument();
    expect(screen.getByText(/such as where a photo was taken/)).toBeInTheDocument();
    expect(screen.getByLabelText("Choose a picture")).toHaveAttribute(
      "accept",
      "image/jpeg,image/png,image/webp",
    );
    expect(screen.queryByRole("button", { name: "Remove picture" })).not.toBeInTheDocument();
  });

  it("sends nothing when the chooser was closed without a file", () => {
    const { server } = open(null);

    fireEvent.change(screen.getByLabelText("Choose a picture"), { target: { files: [] } });

    expect(uploads(server)).toHaveLength(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refuses an SVG before sending it", async () => {
    const { user, server } = open(null);
    const svg = new File(["<svg/>"], "kim.svg", { type: "image/svg+xml" });

    await user.upload(screen.getByLabelText("Choose a picture"), svg);

    expect(await screen.findByRole("alert")).toHaveTextContent("Use a JPEG, PNG or WebP image.");
    expect(uploads(server)).toHaveLength(0);
  });

  it("refuses a file over 5 MB before sending it", async () => {
    const { user, server } = open(null);
    const large = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "kim.png", {
      type: "image/png",
    });

    await user.upload(screen.getByLabelText("Choose a picture"), large);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This picture is larger than 5 MB. Choose a smaller JPEG, PNG or WebP image.",
    );
    expect(uploads(server)).toHaveLength(0);
  });

  it("uploads with the file's type and shows the new picture", async () => {
    const { user, server } = open(null, {
      [`PUT ${PICTURE}`]: { status: 200, body: { etag: "e2" } },
    });
    const exact = new File([new Uint8Array(5 * 1024 * 1024)], "kim.webp", { type: "image/webp" });

    await user.upload(screen.getByLabelText("Choose a picture"), exact);

    expect(await screen.findByText("Your picture is saved.")).toBeInTheDocument();
    expect(server.sent.find((request) => request.method === "PUT")?.headers["content-type"]).toBe(
      "image/webp",
    );
    expect(await screen.findByRole("img", { name: "Kim Lee" })).toContainElement(
      document.querySelector("img"),
    );
    expect(screen.getByRole("button", { name: "Remove picture" })).toBeInTheDocument();
  });

  it("shows the server's sentence when it is busy, and tries again when asked", async () => {
    let calls = 0;
    const { user, server } = open(null, {
      [`PUT ${PICTURE}`]: () => {
        calls += 1;
        return calls === 1
          ? {
              status: 503,
              body: { detail: "The server is busy with other picture uploads. Try again." },
            }
          : { status: 200, body: { etag: "e2" } };
      },
    });

    await user.upload(
      screen.getByLabelText("Choose a picture"),
      new File(["jpg"], "kim.jpg", { type: "image/jpeg" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server is busy with other picture uploads. Try again.",
    );
    expect(uploads(server)).toHaveLength(1);

    // From the keyboard: the pressed button has focus, then goes away.
    screen.getByRole("button", { name: "Try again" }).focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Your picture is saved.")).toBeInTheDocument();
    expect(uploads(server)).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    // The pressed button is gone: focus is on the file field, never on the page behind.
    expect(screen.getByLabelText("Choose a picture")).toHaveFocus();
  });

  it("keeps focus on the file field while a picture uploads, and says it ignored another choice", async () => {
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    const { user, server } = open(null, {
      [`PUT ${PICTURE}`]: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const field = screen.getByLabelText("Choose a picture");
    const png = new File(["png"], "kim.png", { type: "image/png" });

    await user.upload(field, png);
    expect(await screen.findByText("Working…")).toBeInTheDocument();
    expect(field).toHaveFocus();
    expect(field).toBeEnabled();
    expect(field).toHaveAttribute("aria-disabled", "true");
    await user.upload(field, png);
    expect(
      await screen.findByText(
        "A picture is still being sent, so this one was not. Choose it again once the first is saved.",
      ),
    ).toBeInTheDocument();
    answer({ status: 200, body: { etag: "e2" } });

    expect(await screen.findByText("Your picture is saved.")).toBeInTheDocument();
    expect(uploads(server)).toHaveLength(1);
    expect(field).toHaveFocus();
    expect(field).not.toHaveAttribute("aria-disabled");
  });

  it("shows the server's sentence for a file it cannot read, without a retry", async () => {
    const { user } = open(null, {
      [`PUT ${PICTURE}`]: {
        status: 422,
        body: { detail: "The file is not a readable PNG image. Export it again, then retry." },
      },
    });

    await user.upload(
      screen.getByLabelText("Choose a picture"),
      new File(["png"], "kim.png", { type: "image/png" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The file is not a readable PNG image. Export it again, then retry.",
    );
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("removes the picture when one is set", async () => {
    const { user, server } = open("e1", { [`DELETE ${PICTURE}`]: { status: 204 } });

    screen.getByRole("button", { name: "Remove picture" }).focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Your picture is removed.")).toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    expect(screen.queryByRole("button", { name: "Remove picture" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Kim Lee" })).toHaveTextContent("KL");
    expect(screen.getByLabelText("Choose a picture")).toHaveFocus();
  });

  it("shows the pending sign on Try again while the picture is sent again, and nowhere else", async () => {
    let calls = 0;
    const second = held();
    const { user } = open("e1", {
      [`PUT ${PICTURE}`]: () => {
        calls += 1;
        return calls === 1
          ? { status: 503, body: { detail: "The server is busy. Try again." } }
          : second.route();
      },
    });
    await user.upload(
      screen.getByLabelText("Choose a picture"),
      new File(["jpg"], "kim.jpg", { type: "image/jpeg" }),
    );
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expectPending(screen.getByRole("button", { name: /^Try again/ }));
    const remove = screen.getByRole("button", { name: "Remove picture" });
    expect(remove).toBeDisabled();
    expect(remove.querySelector(".pending-sign")).toBeNull();
    second.release({ status: 200, body: { etag: "e2" } });
    expect(await screen.findByText("Your picture is saved.")).toBeInTheDocument();
  });

  it("shows no sign on Remove picture while a chosen file is sent", async () => {
    const { user } = open("e1", { [`PUT ${PICTURE}`]: held().route });

    await user.upload(
      screen.getByLabelText("Choose a picture"),
      new File(["png"], "kim.png", { type: "image/png" }),
    );

    const remove = screen.getByRole("button", { name: "Remove picture" });
    expect(remove).toBeDisabled();
    expect(remove.querySelector(".pending-sign")).toBeNull();
  });

  it("hands focus to the file field while Remove picture works, before the button is disabled", async () => {
    let answer: (value: { status: number }) => void = () => undefined;
    const { user } = open("e1", {
      [`DELETE ${PICTURE}`]: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const remove = screen.getByRole("button", { name: "Remove picture" });
    remove.focus();

    await user.keyboard("{Enter}");

    expect(await screen.findByText("Working…")).toBeInTheDocument();
    expectPending(remove);
    expect(screen.getByLabelText("Choose a picture")).toHaveFocus();
    answer({ status: 204 });
    expect(await screen.findByText("Your picture is removed.")).toBeInTheDocument();
  });

  it("closes with Done", async () => {
    const { user, onClose } = open(null);

    await user.click(screen.getByRole("button", { name: "Done" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
