import { expect, type Locator, type Page, test } from "@playwright/test";

import { stubAccess, stubPolicies, stubSignInAndConfiguration, stubTokensSecrets } from "./access";
import { PROD_RULE, stubCiTrust } from "./ciTrust";
import { PROD, stubConnections, WAREHOUSE } from "./connections";
import { stubEnvironments } from "./environments";
import { FAILED_ID, stubJobs, TIMED_OUT } from "./jobs";
import { stubProvider } from "./oidc";
import { SALES_SQL, stubQueries } from "./queries";
import { stubRepositories } from "./repositories";
import { signInAsAdmin } from "./session";
import { SHOTS } from "./visual/shots";

// One compact density, full-width screens and the alignment rules of #782
// (docs/agent/still-water.md, "Layout and rhythm"), measured in the browser.

/** A screen: its name, how its routes are answered, its address and what shows it has loaded. */
interface Screen {
  /** The screen's name in the test's title. */
  readonly name: string;
  /** Answers its routes like a server would. */
  readonly stub: (page: Page) => Promise<unknown>;
  /** Its address. */
  readonly path: string;
  /** What is visible once the screen has loaded. */
  readonly loaded: (page: Page) => Locator;
}

const SCREENS: readonly Screen[] = [
  {
    name: "Catalog",
    stub: signInAsAdmin,
    path: "/catalog?env=prod&ns=gold.sales",
    loaded: (page) => page.getByRole("region", { name: "Tables" }),
  },
  {
    name: "Queries",
    stub: stubQueries,
    path: "/queries",
    loaded: (page) => page.getByRole("textbox", { name: "SQL" }),
  },
  {
    name: "Jobs",
    stub: stubJobs,
    path: "/jobs",
    loaded: (page) => page.locator(".jobs-list tbody tr").first(),
  },
  {
    name: "Connections",
    stub: stubConnections,
    path: "/connections",
    loaded: (page) => page.locator(".connections-scroll tbody tr").first(),
  },
  {
    name: "Settings › Environments",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubEnvironments(page);
    },
    path: "/settings/environments",
    loaded: (page) => page.locator(".environments-scroll tbody tr").first(),
  },
  {
    name: "Access › People",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubAccess(page);
    },
    path: "/settings/access",
    loaded: (page) => page.locator(".list tbody tr").first(),
  },
  {
    name: "Row filters & masks",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubPolicies(page);
    },
    path: "/settings/access/filters-and-masks",
    loaded: (page) => page.locator(".policies-scroll tbody tr").first(),
  },
  {
    name: "Access › Grants",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubAccess(page);
    },
    path: "/settings/access/grants",
    loaded: (page) => page.getByRole("region", { name: "Grants" }),
  },
];

/**
 * What scrolls sideways: the page, and every list's own region, each with how far it scrolls.
 *
 * @param page - The page.
 * @returns The page's overflow and each list region's, in pixels.
 */
function sideways(page: Page): Promise<{ page: number; lists: number[] }> {
  return page.evaluate(() => ({
    page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    lists: [...document.querySelectorAll(".list-scroll")].map(
      (region) => region.scrollWidth - region.clientWidth,
    ),
  }));
}

for (const width of [1280, 1440]) {
  for (const screen of SCREENS) {
    test(`${screen.name} at ${String(width)} px: the full width, no sideways scroll, names on one line`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await screen.stub(page);
      await page.goto(screen.path);
      await expect(screen.loaded(page)).toBeVisible();

      const { page: overflow, lists } = await sideways(page);
      expect(overflow, "the page").toBeLessThanOrEqual(0);
      for (const [index, list] of lists.entries()) {
        expect(list, `list region ${String(index)}`).toBeLessThanOrEqual(0);
      }

      // The page fills its place: the working area less its gutter, or beside Settings' list.
      const [content, place] = await page.locator(".page").evaluate((node) => {
        const parent = node.parentElement;
        const style = parent === null ? null : getComputedStyle(parent);
        const inner =
          parent === null || style === null
            ? 0
            : parent.clientWidth -
              Number.parseFloat(style.paddingLeft) -
              Number.parseFloat(style.paddingRight);
        return [node.getBoundingClientRect().width, inner];
      });
      expect(content).toBeGreaterThan(place - 1);
      const area = (await page.locator(".shell-main").boundingBox())?.width ?? 0;
      expect(place).toBeGreaterThan(area / 2);

      // A list's name never wraps: it is one line, cut with an ellipsis when it is too long.
      const wrapping = await page
        .locator(".list .row-name")
        .evaluateAll(
          (names) => names.filter((name) => getComputedStyle(name).whiteSpace !== "nowrap").length,
        );
      expect(wrapping).toBe(0);
    });
  }
}

test("a cut name ends with an ellipsis and shows whole in a tooltip on hover and focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await signInAsAdmin(page);
  await stubAccess(page, { longNames: true });
  await page.goto("/settings/access/groups/analysts");
  // A member with a 300-character name.
  const name = page.locator(".list a.row-name").filter({ hasText: "maria_xxx" }).first();
  await expect(name).toBeVisible();
  const look = await name.evaluate((node) => ({
    cut: node.scrollWidth > node.clientWidth,
    ellipsis: getComputedStyle(node).textOverflow,
    height: node.getBoundingClientRect().height,
  }));
  expect(look.cut).toBe(true);
  expect(look.ellipsis).toBe("ellipsis");
  expect(look.height).toBeLessThan(24);

  const tip = page.locator(".truncation-tip");
  await name.hover();
  await expect(tip).toContainText("maria_xxx");
  await page.keyboard.press("Escape");
  await expect(tip).toHaveCount(0);

  await name.focus();
  await expect(tip).toContainText("maria_xxx");
  await page.keyboard.press("Tab");
  await expect(tip).toHaveCount(0);
});

test("the query result's two downloads are one height and 8 px apart", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await stubQueries(page);
  await page.goto("/queries");
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  await page.keyboard.insertText(SALES_SQL);
  await page.keyboard.press("ControlOrMeta+Enter");
  const csv = page.getByRole("button", { name: "Download CSV" });
  const xlsx = page.getByRole("button", { name: "Download spreadsheet" });
  await expect(xlsx).toBeVisible();

  const first = await csv.boundingBox();
  const second = await xlsx.boundingBox();
  expect(first?.height).toBe(32);
  expect(second?.height).toBe(32);
  expect(Math.round((second?.x ?? 0) - ((first?.x ?? 0) + (first?.width ?? 0)))).toBe(8);
  expect(Math.round(second?.y ?? 0)).toBe(Math.round(first?.y ?? 0));
});

test("the primary action sits at the top right, centred on the title's line", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await stubConnections(page);
  await page.goto("/connections");
  const title = page.getByRole("heading", { level: 1, name: "Connections" });
  await expect(title).toBeVisible();
  const action = page.locator(".page-head > :not(h1)").first();
  await expect(action).toBeVisible();
  const head = await title.boundingBox();
  const act = await action.boundingBox();
  const centre = (box: typeof head): number => (box?.y ?? 0) + (box?.height ?? 0) / 2;
  expect(Math.abs(centre(head) - centre(act))).toBeLessThanOrEqual(1);
  const main = await page.locator(".page").boundingBox();
  expect(
    Math.abs((act?.x ?? 0) + (act?.width ?? 0) - ((main?.x ?? 0) + (main?.width ?? 0))),
  ).toBeLessThanOrEqual(1);
});

test("controls in one row share one height", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await signInAsAdmin(page);
  await stubAccess(page);
  await page.goto("/settings/access/grants");
  await expect(page.getByRole("region", { name: "Grants" })).toBeVisible();
  // The filters above the grants: three pickers in a row.
  const heights = await page
    .locator(".access-filters .button")
    .evaluateAll((controls) => controls.map((control) => control.getBoundingClientRect().height));
  expect(heights.length).toBeGreaterThan(1);
  expect(new Set(heights)).toEqual(new Set([32]));
});

test("a state is a small pill: its glyph and its word on a tinted ground", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await stubJobs(page);
  await page.goto("/jobs/runs");
  const pill = page.locator(".list .pill").first();
  await expect(pill).toBeVisible();
  const box = await pill.boundingBox();
  expect(box?.height ?? 0).toBeLessThanOrEqual(20);
  await expect(pill.locator("svg")).toHaveAttribute("width", "12");
});

/** A detail page with a breadcrumb, and the list it is reached from. */
const DETAILS: readonly {
  name: string;
  stub: (page: Page) => Promise<unknown>;
  from: string;
  to: string;
}[] = [
  {
    name: "a table",
    stub: signInAsAdmin,
    from: "/catalog?env=prod&ns=gold.sales",
    to: "/catalog/prod/gold.sales/fact_sales",
  },
  { name: "a run", stub: stubJobs, from: "/jobs", to: `/jobs/prod/runs/${FAILED_ID}` },
  {
    name: "a task",
    stub: stubJobs,
    from: `/jobs/prod/runs/${FAILED_ID}`,
    to: `/jobs/prod/runs/${FAILED_ID}/tasks/${TIMED_OUT}`,
  },
  {
    name: "a connection",
    stub: stubConnections,
    from: "/connections",
    to: `/connections/${PROD}/${WAREHOUSE}`,
  },
  {
    name: "an environment",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubEnvironments(page);
    },
    from: "/settings/environments",
    to: "/settings/environments/prod",
  },
  {
    name: "a CI trust rule",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubCiTrust(page);
    },
    from: "/settings/ci-trust",
    to: `/settings/ci-trust/${PROD_RULE}`,
  },
  {
    name: "a group",
    stub: async (page) => {
      await signInAsAdmin(page);
      await stubAccess(page);
    },
    from: "/settings/access/groups",
    to: "/settings/access/groups/analysts",
  },
];

for (const detail of DETAILS) {
  test(`Alt+Left goes back from ${detail.name}'s page, which has a breadcrumb`, async ({
    page,
  }) => {
    await detail.stub(page);
    await page.goto(detail.from);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await page.goto(detail.to);
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toBeVisible();
    // The focus is on the page's heading after it opened; nothing has used the key.
    await page.getByRole("heading", { level: 1 }).focus();

    await page.keyboard.press("Alt+ArrowLeft");

    await expect(page).toHaveURL((url) => `${url.pathname}${url.search}` === detail.from);
  });
}

test("Alt+Left in the SQL editor stays in the editor; on a page without a breadcrumb it goes back", async ({
  page,
}) => {
  await stubQueries(page);
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await page.goto("/queries");
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.click();
  await page.keyboard.insertText("SELECT 1");

  await page.keyboard.press("Alt+ArrowLeft");
  await expect(page).toHaveURL(/\/queries$/);

  // Out of the editor, Queries (no breadcrumb) is left like any page: Alt+Left is global.
  await page.getByRole("heading", { level: 1, name: "Queries" }).focus();
  await page.keyboard.press("Alt+ArrowLeft");
  await expect(page).toHaveURL(/\/jobs$/);
});

test("Alt+Left does nothing while a dialog or a menu is open", async ({ page }) => {
  await signInAsAdmin(page);
  await page.goto("/catalog?env=prod&ns=gold.sales");
  await expect(page.getByRole("region", { name: "Tables" })).toBeVisible();
  await page.goto("/catalog/prod/gold.sales/fact_sales");
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toBeVisible();

  await page.getByRole("button", { name: "Actions" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Alt+ArrowLeft");
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Alt+ArrowLeft");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page).toHaveURL(/\/catalog\/prod\/gold\.sales\/fact_sales$/);
});

/**
 * Signs in as an administrator, then answers the routes of one more stub.
 *
 * @param stub - The screen's stub.
 * @returns What a test calls with its page.
 */
function admin(stub: (page: Page) => Promise<unknown>): (page: Page) => Promise<void> {
  return async (page) => {
    await signInAsAdmin(page);
    await stub(page);
  };
}

/** A list screen with the shared toolbar, and whether its toolbar carries the list's count. */
const TOOLBARS: readonly [string, (page: Page) => Promise<unknown>, string, boolean][] = [
  ["Connections", stubConnections, "/connections", true],
  ["Jobs", stubJobs, "/jobs", false],
  ["Runs", stubJobs, "/jobs/runs", true],
  ["Deploys", stubJobs, "/jobs/deploys", true],
  ["Environments", admin(stubEnvironments), "/settings/environments", true],
  ["Repositories", admin(stubRepositories), "/settings/repositories", true],
  ["CI trust", admin(stubCiTrust), "/settings/ci-trust", true],
  ["People", admin(stubAccess), "/settings/access", true],
  ["Groups", admin(stubAccess), "/settings/access/groups", true],
  ["Grants", admin(stubAccess), "/settings/access/grants", true],
  ["Tokens", admin(stubTokensSecrets), "/settings/access/tokens", true],
  ["Secrets", admin(stubTokensSecrets), "/settings/access/secrets?environment=prod", true],
  ["Row filters & masks", admin(stubPolicies), "/settings/access/filters-and-masks", true],
  ["Invites", admin(stubSignInAndConfiguration), "/settings/sign-in/invites", true],
  ["Recovery links", admin(stubSignInAndConfiguration), "/settings/sign-in/recovery-links", true],
  ["Configuration", admin(stubSignInAndConfiguration), "/settings/configuration", true],
];

for (const [name, stub, path, counted] of TOOLBARS) {
  test(`${name}: the toolbar's count on the left, Refresh on the right, its controls one height`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page);
    await page.goto(path);
    const bar = page.locator(".list-toolbar").first();
    await expect(bar).toBeVisible();
    const count = bar.locator(".toolbar-count");
    if (counted) {
      await expect(count).toHaveText(/^(\d[\d,]* \S|The first \d)/);
    } else {
      await expect(count).toHaveCount(0);
    }
    // The list's search, filters and Refresh: one height.
    const heights = await bar
      .locator("input, .button, button")
      .evaluateAll((controls) =>
        controls
          .filter((control) => control.getClientRects().length > 0)
          .map((control) => Math.round(control.getBoundingClientRect().height)),
      );
    expect(heights.length, "controls").toBeGreaterThan(0);
    expect(new Set(heights), heights.join(" ")).toEqual(new Set([32]));
    const refresh = bar.getByRole("button", { name: "Refresh" });
    if ((await refresh.count()) > 0) {
      const box = await refresh.boundingBox();
      const own = await bar.boundingBox();
      expect(
        Math.abs((box?.x ?? 0) + (box?.width ?? 0) - ((own?.x ?? 0) + (own?.width ?? 0))),
      ).toBeLessThanOrEqual(1);
    }
  });
}

/** The list screens whose rows open their object, so end in a `›`. */
const WITH_CHEVRON = new Set([
  "Connections",
  "Jobs",
  "Runs",
  "Environments",
  "Repositories",
  "People",
  "Groups",
  "Recovery links",
]);

for (const [name, stub, path] of TOOLBARS.filter(([screen]) => WITH_CHEVRON.has(screen))) {
  test(`${name}: each row's › ends at the list's right edge`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await stub(page);
    await page.goto(path);
    await expect(page.locator(".list tbody tr").first()).toBeVisible();
    const gaps = await page.locator(".list").evaluateAll((lists) =>
      lists.flatMap((list) => {
        const right = list.getBoundingClientRect().right;
        return [...list.querySelectorAll(".chevron")]
          .filter((mark) => mark.getClientRects().length > 0)
          .map((mark) => right - mark.getBoundingClientRect().right);
      }),
    );
    // Within the cell's 12 px padding of the edge, in every list that has the mark.
    expect(gaps.length, "rows with a ›").toBeGreaterThan(0);
    for (const gap of gaps) {
      expect(gap).toBeLessThanOrEqual(12.5);
    }
  });
}

for (const width of [1280, 1440]) {
  test(`Row filters & masks at ${String(width)} px: who, the column and the time stay on one line`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await signInAsAdmin(page);
    await stubPolicies(page);
    await page.goto("/settings/access/filters-and-masks");
    await expect(page.locator(".policies-scroll tbody tr").first()).toBeVisible();
    // The expression is code and wraps by design; the names and the time beside it never do.
    const tall = await page
      .locator(".policies-scroll .policy-who, .policies-scroll .row-name, .policies-scroll time")
      .evaluateAll((cells) =>
        cells
          // A service account's second line is meant to be there.
          .filter((cell) => cell.querySelector(".second") === null)
          .filter((cell) => cell.getBoundingClientRect().height > 0)
          .filter(
            (cell) =>
              cell.getBoundingClientRect().height >
              Number.parseFloat(getComputedStyle(cell).lineHeight) * 1.5,
          )
          .map((cell) => cell.textContent),
      );
    expect(tall).toEqual([]);
  });
}

/**
 * Opens one of the visual suite's shots (its stubs, its address, and what shows it has loaded).
 *
 * @param page - The page.
 * @param name - The shot's name.
 */
async function openShot(page: Page, name: string): Promise<void> {
  const shot = SHOTS.find((each) => each.name === name);
  if (shot === undefined) {
    throw new Error(`no shot ${name}`);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await shot.stub(page);
  await page.goto(shot.path);
  await expect(shot.ready(page)).toBeVisible();
  // Measured in the bundled fonts, not the fallback they swap from (as the visual suite does).
  await page.evaluate(() => document.fonts.ready);
}

/**
 * How far an element falls short of its parent's content box, on the right, in pixels.
 *
 * @param block - The element.
 * @returns The gap between its right edge and its parent's content edge.
 */
function shortOfColumn(block: Locator): Promise<number> {
  return block.evaluate((node) => {
    const parent = node.parentElement;
    if (parent === null) {
      return Number.POSITIVE_INFINITY;
    }
    const inner =
      parent.getBoundingClientRect().right -
      Number.parseFloat(getComputedStyle(parent).paddingRight) -
      Number.parseFloat(getComputedStyle(parent).borderRightWidth);
    return inner - node.getBoundingClientRect().right;
  });
}

// #820: a paragraph that is a block (a banner, a notice, a note) spans its column; only running
// text keeps the reading width.
for (const [shot, selector] of [
  ["catalog-error", ".page p.step-error"],
  ["home-no-access", ".page p.page-notice"],
  ["home", ".page .home-update > p"],
] as const) {
  test(`${shot}: ${selector} spans its column`, async ({ page }) => {
    await openShot(page, shot);
    const block = page.locator(selector).first();
    await expect(block).toBeVisible();
    // The update note's sentence ends where its × begins; the × sits at the note's end.
    const measured = shot === "home" ? page.locator(".home-update-hide") : block;
    expect(Math.abs(await shortOfColumn(measured))).toBeLessThanOrEqual(0.5);
  });
}

test("Settings › Sign-in's warning (a .step-warning) spans its column", async ({ page }) => {
  // The warning shows when at most one administrator can still sign in with a passkey.
  await stubProvider(page, { unlinkedAdmins: 1 });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/settings/sign-in");
  const warning = page.locator(".page p.step-warning");
  await expect(warning).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  expect(Math.abs(await shortOfColumn(warning))).toBeLessThanOrEqual(0.5);
});

test("running text keeps the reading width", async ({ page }) => {
  await openShot(page, "settings-environments");
  const widths = await page
    .locator(".page p.page-purpose")
    .evaluateAll((lines) => lines.map((line) => getComputedStyle(line).maxWidth));
  expect(widths.length).toBeGreaterThan(0);
  expect(new Set(widths)).toEqual(new Set(["720px"]));
});
