import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useReloadViewer, useSettingsViewer } from "./viewer";

describe("the Settings viewer outside Settings", () => {
  it("is nobody, and reading it again does nothing", () => {
    const { result } = renderHook(() => ({
      viewer: useSettingsViewer(),
      reload: useReloadViewer(),
    }));

    expect(result.current.viewer.secretEnvironments).toBeNull();
    expect(() => {
      result.current.reload();
    }).not.toThrow();
  });
});
