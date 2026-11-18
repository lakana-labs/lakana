import { describe, expect, it } from "vitest";

import { connectionPath, editPath, NEW_CONNECTION_PATH } from "./paths";

describe("the addresses of the Connections screen", () => {
  it("names the page that adds a connection", () => {
    expect(NEW_CONNECTION_PATH).toBe("/connections/new");
  });

  it("encodes each part of a connection's address and its edit address", () => {
    expect(connectionPath("prod", "crm")).toBe("/connections/prod/crm");
    expect(connectionPath("a/b", "c d")).toBe("/connections/a%2Fb/c%20d");
    expect(editPath("prod", "crm")).toBe("/connections/prod/crm/edit");
  });
});
