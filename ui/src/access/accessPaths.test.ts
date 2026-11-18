import { describe, expect, it } from "vitest";

import { type GrantFilter, NO_FILTER } from "../api/grants";
import { filterKey, grantsPath, groupPath, readGrantFilter } from "./accessPaths";

/**
 * Reads back the filter of an address.
 *
 * @param path - The address.
 * @returns The filter.
 */
function readBack(path: string): GrantFilter {
  return readGrantFilter(new URLSearchParams(path.split("?")[1] ?? ""));
}

describe("paths", () => {
  it.each<GrantFilter>([
    NO_FILTER,
    {
      who: { kind: "group", name: "analysts" },
      what: { kind: "role", role: "ANALYST" },
      where: { kind: "TABLE", environment: "dev", namespace: "sales", table: "orders" },
    },
    {
      who: { kind: "principal", name: "maria" },
      what: { kind: "action", action: "READ_TABLE" },
      where: { kind: "NAMESPACE", environment: "dev", namespace: "a.b" },
    },
    { ...NO_FILTER, where: { kind: "ENVIRONMENT", environment: "dev" } },
    { ...NO_FILTER, where: { kind: "INSTANCE" } },
  ])("reads back every filter it writes (%#)", (filter) => {
    expect(readBack(grantsPath(filter))).toEqual(filter);
  });

  it("keeps nothing it cannot read", () => {
    expect(readBack("/x?principal=maria&group=analysts")).toEqual(NO_FILTER);
    expect(readBack("/x?role=OWNER&action=FLY")).toEqual(NO_FILTER);
    expect(readBack("/x?role=ANALYST&action=READ_TABLE")).toEqual(NO_FILTER);
    expect(readBack("/x?namespace=sales&table=orders&principal=")).toEqual(NO_FILTER);
  });

  it("writes the list's address and a group's", () => {
    expect(grantsPath({})).toBe("/settings/access/grants");
    expect(grantsPath({ who: { kind: "principal", name: "maria" } })).toBe(
      "/settings/access/grants?principal=maria",
    );
    expect(groupPath("an al")).toBe("/settings/access/groups/an%20al");
  });

  it("keys the same filter the same way, however it was built", () => {
    const one: GrantFilter = {
      where: { kind: "ENVIRONMENT", environment: "dev" },
      what: null,
      who: { name: "maria", kind: "principal" },
    };
    expect(filterKey(one)).toBe(filterKey(readBack(grantsPath(one))));
    expect(filterKey(NO_FILTER)).not.toBe(filterKey(one));
  });
});
