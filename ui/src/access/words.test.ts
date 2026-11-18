import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { ACTIONS, ROLES } from "../api/grants";
import { actionWords, roleName, roleSummary, whereText } from "./words";

const t = i18next.t.bind(i18next);

describe("words", () => {
  it("names every action and role", () => {
    for (const action of ACTIONS) {
      expect(actionWords(t, action)).not.toContain("access.");
    }
    for (const role of ROLES) {
      expect(roleName(t, role)).toBe(role);
      expect(roleSummary(t, role)).not.toContain("access.");
    }
    expect(actionWords(t, "SUBMIT_QUERY")).toBe("Run queries");
    expect(roleSummary(t, "ANALYST")).toBe("Read tables and run queries");
  });

  it("writes where a grant is", () => {
    const none = { environment: null, namespace: null, table: null };
    expect(whereText(t, { ...none, kind: "INSTANCE" })).toBe("The whole instance");
    expect(whereText(t, { ...none, kind: "ENVIRONMENT", environment: "dev" })).toBe("dev");
    expect(
      whereText(t, { kind: "NAMESPACE", environment: "dev", namespace: "a.b", table: null }),
    ).toBe("dev › a.b");
    expect(
      whereText(t, { kind: "TABLE", environment: "dev", namespace: "a.b", table: "orders" }),
    ).toBe("dev › a.b › orders");
    expect(whereText(t, { ...none, kind: "ENVIRONMENT", environment: "d‮ev" })).toBe("dU+202Eev");
  });
});
