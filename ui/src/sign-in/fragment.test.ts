import { afterEach, describe, expect, it, vi } from "vitest";

import { takeLinkFragment } from "./fragment";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("takeLinkFragment", () => {
  it("takes the link from the address and removes it from history at once", () => {
    window.history.replaceState({ kept: true }, "", "/sign-in?x=1#l=pub-1&s=sec-1");
    const replace = vi.spyOn(window.history, "replaceState");

    const link = takeLinkFragment(window.location, window.history);

    expect(link).toEqual({ linkId: "pub-1", secret: "sec-1" });
    expect(replace).toHaveBeenCalledWith({ kept: true }, "", "/sign-in?x=1");
    expect(window.location.href).not.toContain("sec-1");
    expect(window.location.hash).toBe("");
    replace.mockRestore();
  });

  it("removes an incomplete link too, and hands back what it had", () => {
    window.history.replaceState(null, "", "/sign-in#s=sec-1");

    expect(takeLinkFragment(window.location, window.history)).toEqual({
      linkId: "",
      secret: "sec-1",
    });
    expect(window.location.hash).toBe("");
    window.history.replaceState(null, "", "/sign-in#l=pub-1");
    expect(takeLinkFragment(window.location, window.history)).toEqual({
      linkId: "pub-1",
      secret: "",
    });
  });

  it.each(["/sign-in", "/sign-in#", "/sign-in#other=1", "/catalog#l=pub-1&s=sec-1"])(
    "finds no link at %s",
    (address) => {
      window.history.replaceState(null, "", address);

      expect(takeLinkFragment(window.location, window.history)).toBeNull();
    },
  );
});
