import { renderHook } from "@testing-library/react";
import type { JSX, ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { SettingsViewer } from "../settings/places";
import { SettingsViewerContext } from "../settings/viewer";
import { useGroupSearch, usePrincipalSearch } from "./suggest";

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
