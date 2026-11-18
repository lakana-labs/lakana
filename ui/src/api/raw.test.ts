import { describe, expect, it } from "vitest";

import { listRaw } from "./raw";

import { rawRoutes } from "../test/raw";
import { fakeServer } from "../test/server";

describe("raw api", () => {
  it("asks for the folder, the page size and the cursor, and reads the page", async () => {
    const server = fakeServer(rawRoutes());

    const first = await listRaw("prod", "raw", null);
    await listRaw("prod", "raw/feeds", "c1");

    expect(first.path).toBe("raw");
    expect(first.items.map((entry) => entry.name)).toEqual([
      "events.csv",
      "feeds",
      "orders",
      "payments",
    ]);
    expect(first.nextCursor).toBeNull();
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/prod/raw?path=raw&limit=100",
      "/api/v1/environments/prod/raw?path=raw%2Ffeeds&limit=100&cursor=c1",
    ]);
  });
});
