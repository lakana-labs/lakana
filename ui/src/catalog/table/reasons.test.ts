import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { AccessOut, PermissionOut, ViaOut } from "../../api";
import { table } from "../../test/table";
import { allowedBecause, editTextsAnswer, grantListCommand, namespaceLevels } from "./reasons";

const t = i18next.t.bind(i18next);

const KIM: AccessOut = {
  name: "kim.schulz",
  admin: false,
  noAccessYet: false,
  mayDeploy: false,
  picture: null,
};

/**
 * A way a permission reaches the person.
 *
 * @param overrides - The fields that differ from a role granted on the table.
 * @returns The way.
 */
function via(overrides: Partial<ViaOut> = {}): ViaOut {
  return {
    kind: "role",
    grant_id: "6d5c4b3a-2f1e-4d0c-9b8a-7f6e5d4c3b01",
    group: null,
    role: "ANALYST",
    scope: { kind: "TABLE", environment: "prod", namespace: "gold.sales", table: "fact_sales" },
    ...overrides,
  };
}

/**
 * A permission.
 *
 * @param allowed - The answer.
 * @param ways - How it reaches the person.
 * @returns The permission to drop the table.
 */
function drop(allowed: boolean, ways: readonly ViaOut[] = []): PermissionOut {
  return {
    action: "DROP_TABLE",
    allowed,
    via: ways,
    reason: allowed ? null : "No grant gives it.",
  };
}

describe("allowedBecause", () => {
  it("names a role, the object it is on and the environment", () => {
    expect(allowedBecause(drop(true, [via()]), t)).toBe(
      "Through the Analyst role on table gold.sales.fact_sales in prod.",
    );
  });

  it("names a single action granted on a namespace, an environment or the instance", () => {
    const ways = [
      via({
        kind: "grant",
        role: null,
        scope: { kind: "NAMESPACE", environment: "prod", namespace: "gold", table: null },
      }),
      via({
        kind: "grant",
        role: null,
        scope: { kind: "ENVIRONMENT", environment: "prod", namespace: null, table: null },
      }),
      via({
        role: "ADMIN",
        scope: { kind: "INSTANCE", environment: null, namespace: null, table: null },
      }),
    ];

    expect(allowedBecause(drop(true, ways), t)).toBe(
      "Through a grant of DROP_TABLE on namespace gold in prod. " +
        "Through a grant of DROP_TABLE on environment prod. " +
        "Through the Admin role on the whole instance.",
    );
  });

  it("names the group a grant reaches the person through", () => {
    const ways = [
      via({ kind: "group", group: "analysts", role: "DATA_ENGINEER" }),
      via({ kind: "group", group: "loaders", role: null }),
    ];

    expect(allowedBecause(drop(true, ways), t)).toBe(
      "Through the group analysts, which has the Data engineer role on table gold.sales.fact_sales in prod. " +
        "Through the group loaders, which has a grant of DROP_TABLE on table gold.sales.fact_sales in prod.",
    );
  });

  it("says each reason once", () => {
    expect(allowedBecause(drop(true, [via(), via({ grant_id: "other" })]), t)).toBe(
      "Through the Analyst role on table gold.sales.fact_sales in prod.",
    );
  });

  it("says the server's reason, else to ask an administrator, for a refusal", () => {
    expect(allowedBecause(drop(false), t)).toBe("No grant gives it.");
    expect(allowedBecause({ ...drop(false), reason: null }, t)).toBe("Ask an administrator.");
  });

  it("still says something for a permission allowed without a way the server names", () => {
    expect(allowedBecause(drop(true), t)).toBe("Allowed for you.");
  });
});

describe("editTextsAnswer", () => {
  it("says you may edit texts because you own the table", () => {
    expect(
      editTextsAnswer(
        table({ owner: { name: "kim.schulz", kind: "USER" } }),
        KIM,
        true,
        drop(false),
        t,
      ),
    ).toEqual({
      allowed: true,
      words: "You own the table.",
    });
  });

  it("says you may edit texts because you may drop the table, and why you may", () => {
    expect(editTextsAnswer(table(), KIM, true, drop(true, [via()]), t)).toEqual({
      allowed: true,
      words:
        "You may drop the table, so you may edit its texts. Through the Analyst role on table gold.sales.fact_sales in prod.",
    });
  });

  it("tells someone who may not edit texts to ask the owner", () => {
    expect(editTextsAnswer(table(), KIM, false, drop(false), t)).toEqual({
      allowed: false,
      words:
        "Only the owner (ana.nowak) or someone who may drop the table may edit its texts. Ask the owner.",
    });
  });

  it("tells someone who may not edit the texts of a table without an owner to ask an administrator", () => {
    expect(editTextsAnswer(table({ owner: null }), KIM, false, undefined, t)).toEqual({
      allowed: false,
      words:
        "Only someone who may drop the table may edit its texts, and the table has no owner. Ask an administrator.",
    });
  });
});

describe("editTextsAnswer and the page's rule", () => {
  it("follows whether the page lets the person edit, not a rule of its own", () => {
    expect(editTextsAnswer(table(), KIM, false, drop(true, [via()]), t).allowed).toBe(false);
    expect(
      editTextsAnswer(
        table({ owner: { name: "kim.schulz", kind: "USER" } }),
        KIM,
        false,
        undefined,
        t,
      ).allowed,
    ).toBe(false);
    expect(editTextsAnswer(table(), KIM, true, undefined, t)).toEqual({
      allowed: true,
      words: "You may drop the table, so you may edit its texts.",
    });
  });
});

describe("namespaceLevels", () => {
  it("lists the table's namespace and each one above it, the nearest first", () => {
    expect(namespaceLevels("gold.sales.eu")).toEqual(["gold.sales.eu", "gold.sales", "gold"]);
    expect(namespaceLevels("gold")).toEqual(["gold"]);
  });
});

describe("grantListCommand", () => {
  it("names the CLI command that lists every grant on one level", () => {
    expect(grantListCommand("prod", "gold.sales", "fact_sales")).toBe(
      "lakana grant list --env 'prod' --ns 'gold.sales' --table 'fact_sales'",
    );
    expect(grantListCommand("prod", "gold", null)).toBe(
      "lakana grant list --env 'prod' --ns 'gold'",
    );
    expect(grantListCommand("prod", null, null)).toBe("lakana grant list --env 'prod'");
    expect(grantListCommand("it's", null, null)).toBe("lakana grant list --env 'it'\\''s'");
  });
});
