import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { pictureUrl, removePicture } from "../api";
import { fakeServer } from "../test/server";
import { Avatar } from "./Avatar";

const WEBP = { status: 200, blob: new Blob(["webp"], { type: "image/webp" }) };

describe("Avatar", () => {
  it("shows initials without a picture, and fetches nothing", () => {
    const server = fakeServer();

    const { container } = render(
      <Avatar name="kim.lee" displayName={null} etag={null} size={32} />,
    );

    expect(container.firstElementChild).toHaveTextContent("KL");
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector("img")).toBeNull();
    expect(server.sent).toHaveLength(0);
  });

  it("shows the picture as a blob URL once it arrived, initials until then", async () => {
    fakeServer({ "GET /api/v1/principals/kim.lee/picture": WEBP });

    const { container } = render(
      <Avatar name="kim.lee" displayName="Kim Lee" etag="e1" size={48} />,
    );
    expect(container.firstElementChild).toHaveTextContent("KL");

    await act(async () => {
      await Promise.resolve();
    });
    const image = await screen.findByRole("presentation", { hidden: true });
    expect(image.getAttribute("src")).toMatch(/^blob:/);
    expect(image).toHaveAttribute("alt", "");
  });

  it("goes back to the initials when the picture cannot be shown", async () => {
    fakeServer({ "GET /api/v1/principals/kim.lee/picture": WEBP });
    const { container } = render(<Avatar name="kim.lee" displayName={null} etag="e1" size={24} />);
    const image = await screen.findByRole("presentation", { hidden: true });

    fireEvent.error(image);

    expect(container.querySelector("img")).toBeNull();
    expect(container.firstElementChild).toHaveTextContent("KL");
  });

  it("keeps the initials when the picture cannot be read", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim.lee/picture": { status: 404, body: { detail: "None." } },
    });
    const { container } = render(<Avatar name="kim.lee" displayName={null} etag="e1" size={24} />);

    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });

    expect(container.querySelector("img")).toBeNull();
  });

  it("shows nothing it read after it was gone", async () => {
    const server = fakeServer({ "GET /api/v1/principals/kim.lee/picture": WEBP });
    const { unmount } = render(<Avatar name="kim.lee" displayName={null} etag="e1" size={24} />);

    unmount();

    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });
    await expect(pictureUrl("kim.lee", "e1")).resolves.toMatch(/^blob:/);
  });

  it("is named by the person when no visible name stands beside it", () => {
    render(<Avatar name="kim.lee" displayName="Kim Lee" etag={null} size={48} label="Kim Lee" />);

    expect(screen.getByRole("img", { name: "Kim Lee" })).toHaveTextContent("KL");
  });

  it("follows a picture removed elsewhere in the interface", async () => {
    fakeServer({
      "GET /api/v1/principals/kim.lee/picture": WEBP,
      "DELETE /api/v1/principals/kim.lee/picture": { status: 204 },
    });
    const { container } = render(<Avatar name="kim.lee" displayName={null} etag="e1" size={32} />);
    await screen.findByRole("presentation", { hidden: true });

    await act(() => removePicture("kim.lee"));

    expect(container.querySelector("img")).toBeNull();
  });
});
