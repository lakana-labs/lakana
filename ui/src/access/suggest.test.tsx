import { renderHook } from "@testing-library/react";
import type { JSX, ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { SettingsViewer } from "../settings/places";
import { SettingsViewerContext } from "../settings/viewer";
import { item } from "../test/catalog";
import { fakeServer } from "../test/server";
import { dottedTableSearch, useGroupSearch, usePrincipalSearch } from "./suggest";

/**
 * The viewer's rights around a hook.
 *
 * @param admin - Whether the person is an administrator.
 * @returns The wrapper.
 */
function viewer(admin: boolean): (props: { readonly children: ReactNode }) => JSX.Element {
  const value = { admin, noAccessYet: false } as SettingsViewer;
  return ({ children }) => <SettingsViewerContext value={value}>{children}</SettingsViewerContext>;
}

describe("the searches of Access", () => {
  it("are offered to an administrator", () => {
    expect(renderHook(usePrincipalSearch, { wrapper: viewer(true) }).result.current).not.toBeNull();
    expect(renderHook(useGroupSearch, { wrapper: viewer(true) }).result.current).not.toBeNull();
  });

  it("are not offered to anyone else, who gets a plain field that asks nothing", () => {
    expect(renderHook(usePrincipalSearch, { wrapper: viewer(false) }).result.current).toBeNull();
    expect(renderHook(useGroupSearch, { wrapper: viewer(false) }).result.current).toBeNull();
  });
});

describe("dottedTableSearch", () => {
  /**
   * Asks for a text and gives the request's path and the suggested names.
   *
   * @param text - What is typed.
   * @returns The path and the names.
   */
  async function ask(text: string): Promise<{ path: string; names: string[] }> {
    const server = fakeServer({
      "GET /api/v1/tables": {
        status: 200,
        body: {
          items: [item({ environment: "dev", namespace: "silver.weather", name: "station_daily" })],
          next_cursor: null,
          capped: false,
        },
      },
    });
    const matches = await dottedTableSearch("dev")(text, new AbortController().signal);
    return {
      path: server.sent.at(-1)?.path ?? "",
      names: matches.choices.map((choice) => choice.name),
    };
  }

  it("lists every table of a namespace for its name and a dot", async () => {
    expect(await ask("gold.")).toEqual({
      path: "/api/v1/tables?environment=dev&sort=name&limit=20&namespace=gold",
      names: ["silver.weather.station_daily"],
    });
  });

  it("splits at the last dot, so a namespace may hold dots", async () => {
    expect((await ask("silver.weather.st")).path).toBe(
      "/api/v1/tables?environment=dev&sort=name&limit=20&namespace=silver.weather&name=st",
    );
  });

  it("looks in the names alone for a text that starts with its dot", async () => {
    expect((await ask(".x")).path).toBe(
      "/api/v1/tables?environment=dev&sort=name&limit=20&name=.x",
    );
  });
});
