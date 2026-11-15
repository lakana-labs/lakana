import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { useProtected } from "./useProtected";

describe("useProtected", () => {
  it("is undefined while it reads, then says whether the environment is protected", async () => {
    const slow = held();
    fakeServer({ "GET /api/v1/environments/prod": slow.route });
    const { result } = renderHook(() => useProtected("prod"));

    expect(result.current).toBeUndefined();
    slow.release({ status: 200, body: { name: "prod", is_protected: true } });

    await waitFor(() => {
      expect(result.current).toBe(true);
    });
  });

  it("is null when the environment cannot be read, and for any other failure", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod": { status: 403, body: { detail: "No." } },
      "GET /api/v1/environments/dev": { status: 500, body: { detail: "Broken." } },
    });
    const prod = renderHook(() => useProtected("prod"));
    const dev = renderHook(() => useProtected("dev"));

    await waitFor(() => {
      expect(prod.result.current).toBeNull();
      expect(dev.result.current).toBeNull();
    });
    expect(server.sent.map((request) => request.path).sort()).toEqual([
      "/api/v1/environments/dev",
      "/api/v1/environments/prod",
    ]);
  });

  it("forgets the answer for another environment while it reads", async () => {
    fakeServer({
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      "GET /api/v1/environments/dev": { status: 200, body: { name: "dev", is_protected: false } },
    });
    const { result, rerender } = renderHook(({ name }) => useProtected(name), {
      initialProps: { name: "prod" },
    });
    await waitFor(() => {
      expect(result.current).toBe(true);
    });

    rerender({ name: "dev" });

    expect(result.current).toBeUndefined();
    await waitFor(() => {
      expect(result.current).toBe(false);
    });
  });

  it("drops an answer, or a failure, that arrives for an environment no longer asked about", async () => {
    const prod = held();
    const staging = held();
    fakeServer({
      "GET /api/v1/environments/prod": prod.route,
      "GET /api/v1/environments/staging": staging.route,
      "GET /api/v1/environments/dev": { status: 200, body: { name: "dev", is_protected: false } },
    });
    const { result, rerender } = renderHook(({ name }) => useProtected(name), {
      initialProps: { name: "prod" },
    });
    rerender({ name: "staging" });
    rerender({ name: "dev" });
    await waitFor(() => {
      expect(result.current).toBe(false);
    });

    prod.release({ status: 200, body: { name: "prod", is_protected: true } });
    staging.release({ status: 500, body: { detail: "Broken." } });
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(result.current).toBe(false);
  });
});
