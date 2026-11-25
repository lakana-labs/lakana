import { describe, expect, it } from "vitest";

import {
  type ListSortSpec,
  nextListSort,
  readListSort,
  sortChoice,
  writeListSort,
} from "./listSort";

const RUNS: ListSortSpec<"status" | "name" | "created"> = {
  fields: { status: "asc", name: "asc", created: "desc" },
  natural: { field: "created", order: "desc" },
};

const PEOPLE: ListSortSpec<"name" | "status"> = {
  fields: { name: "asc", status: "asc" },
  natural: null,
};

describe("the sort in the address", () => {
  it("reads a field and a direction, and the list's own order without one", () => {
    expect(readListSort(new URLSearchParams(), RUNS)).toEqual({ field: "created", order: "desc" });
    expect(readListSort(new URLSearchParams(), PEOPLE)).toEqual({ field: null, order: "asc" });
    expect(readListSort(new URLSearchParams("sort=name"), RUNS)).toEqual({
      field: "name",
      order: "asc",
    });
    expect(readListSort(new URLSearchParams("sort=name&order=desc"), RUNS)).toEqual({
      field: "name",
      order: "desc",
    });
  });

  it("ignores a field the list does not have and a direction that is neither asc nor desc", () => {
    expect(readListSort(new URLSearchParams("sort=secret&order=asc"), RUNS)).toEqual({
      field: "created",
      order: "desc",
    });
    expect(readListSort(new URLSearchParams("sort=toString"), PEOPLE).field).toBeNull();
    expect(readListSort(new URLSearchParams("sort=created&order=up"), RUNS).order).toBe("desc");
  });

  it("flips the sorted column and starts another in its own direction", () => {
    const shown = readListSort(new URLSearchParams(), RUNS);
    expect(nextListSort(shown, "created", RUNS)).toEqual({ field: "created", order: "asc" });
    expect(nextListSort(shown, "status", RUNS)).toEqual({ field: "status", order: "asc" });
    expect(nextListSort({ field: "name", order: "asc" }, "name", RUNS)).toEqual({
      field: "name",
      order: "desc",
    });
  });

  it("writes only what differs from the list's own order, and keeps the filters", () => {
    const params = new URLSearchParams("status=failed&sort=name&order=desc");
    expect(writeListSort(params, { field: "created", order: "desc" }, RUNS).toString()).toBe(
      "status=failed",
    );
    expect(writeListSort(params, { field: "created", order: "asc" }, RUNS).toString()).toBe(
      "status=failed&sort=created&order=asc",
    );
    expect(writeListSort(params, { field: "status", order: "asc" }, RUNS).toString()).toBe(
      "status=failed&sort=status",
    );
    expect(writeListSort(params, { field: null, order: "asc" }, PEOPLE).toString()).toBe(
      "status=failed",
    );
  });

  it("sends the field and the direction, and nothing for the list's own order", () => {
    expect(sortChoice({ field: "name", order: "desc" }, RUNS)).toEqual({
      sort: "name",
      order: "desc",
    });
    expect(sortChoice({ field: "created", order: "asc" }, RUNS)).toEqual({
      sort: "created",
      order: "asc",
    });
    expect(sortChoice({ field: "created", order: "desc" }, RUNS)).toBeNull();
    expect(sortChoice({ field: null, order: "asc" }, PEOPLE)).toBeNull();
  });
});
