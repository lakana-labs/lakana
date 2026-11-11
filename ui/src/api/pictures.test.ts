import { afterEach, describe, expect, it, vi } from "vitest";

import { send, setCsrfToken } from "./client";
import {
  changedPicture,
  forgetPictures,
  PICTURE_MAX_BYTES,
  pictureUrl,
  removePicture,
  setPicture,
  watchPictures,
} from "./pictures";

import { fakeServer } from "../test/server";

const PATH = "GET /api/v1/principals/kim.lee/picture";
const WEBP = { status: 200, blob: new Blob(["webp"], { type: "image/webp" }) };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("pictureUrl", () => {
  it("fetches the picture with the CSRF header and answers a blob URL", async () => {
    const server = fakeServer({ [PATH]: WEBP });
    setCsrfToken("csrf-1");

    const url = await pictureUrl("kim.lee", "e1");

    expect(url).toMatch(/^blob:/);
    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("csrf-1");
  });

  it("fetches a picture once for every place that shows it", async () => {
    const server = fakeServer({ [PATH]: WEBP });

    const [first, second] = await Promise.all([
      pictureUrl("kim.lee", "e1"),
      pictureUrl("kim.lee", "e1"),
    ]);

    expect(first).toBe(second);
    expect(server.sent).toHaveLength(1);
  });

  it("revokes the old object URL when the picture changes", async () => {
    fakeServer({ [PATH]: WEBP });
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const old = await pictureUrl("kim.lee", "e1");

    const fresh = await pictureUrl("kim.lee", "e2");

    expect(fresh).not.toBe(old);
    await vi.waitFor(() => {
      expect(revoke).toHaveBeenCalledWith(old);
    });
  });

  it("tries again after a failed read", async () => {
    const server = fakeServer({ [PATH]: { status: 503, body: { detail: "Busy." } } });
    await expect(pictureUrl("kim.lee", "e1")).rejects.toMatchObject({ status: 503 });

    server.route({ [PATH]: WEBP });

    await expect(pictureUrl("kim.lee", "e1")).resolves.toMatch(/^blob:/);
  });

  it("revokes nothing for a read that failed", async () => {
    fakeServer({ [PATH]: { status: 404, body: { detail: "No picture." } } });
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const read = pictureUrl("kim.lee", "e1");

    forgetPictures();

    await expect(read).rejects.toMatchObject({ status: 404 });
    expect(revoke).not.toHaveBeenCalled();
  });

  it("forgets every picture at sign-out and when the session ends", async () => {
    const server = fakeServer({
      [PATH]: WEBP,
      "GET /api/v1/things": { status: 401, body: { detail: "Sign in again." } },
    });
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const url = await pictureUrl("kim.lee", "e1");

    await send("GET", "/api/v1/things").catch(() => undefined);

    await vi.waitFor(() => {
      expect(revoke).toHaveBeenCalledWith(url);
    });
    await pictureUrl("kim.lee", "e1");
    expect(server.sent.filter((request) => request.path.endsWith("/picture"))).toHaveLength(2);
  });
});

describe("setPicture and removePicture", () => {
  it("uploads with the file's type and tells every place the new etag", async () => {
    const server = fakeServer({
      "PUT /api/v1/principals/kim.lee/picture": { status: 200, body: { etag: "e2" } },
    });
    const listener = vi.fn();
    const stop = watchPictures(listener);
    const file = new File(["png"], "kim.png", { type: "image/png" });

    await expect(setPicture("kim.lee", file)).resolves.toBe("e2");

    expect(server.sent[0]?.headers["content-type"]).toBe("image/png");
    expect(server.sent[0]?.raw).toBe(file);
    expect(changedPicture("kim.lee")).toBe("e2");
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
  });

  it("removes a picture and tells every place there is none", async () => {
    fakeServer({ [PATH]: WEBP, "DELETE /api/v1/principals/kim.lee/picture": { status: 204 } });
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const url = await pictureUrl("kim.lee", "e1");

    await removePicture("kim.lee");

    expect(changedPicture("kim.lee")).toBeNull();
    expect(changedPicture("someone.else")).toBeUndefined();
    await vi.waitFor(() => {
      expect(revoke).toHaveBeenCalledWith(url);
    });
    forgetPictures();
    expect(changedPicture("kim.lee")).toBeUndefined();
  });

  it("allows what the server allows: 5 MB", () => {
    expect(PICTURE_MAX_BYTES).toBe(5 * 1024 * 1024);
  });
});
