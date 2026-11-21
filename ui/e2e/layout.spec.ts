import { expect, type Locator, type Page, test } from "@playwright/test";

import { stubAccess } from "./access";
import { PROD, stubConnections, WAREHOUSE } from "./connections";
import { stubEnvironments } from "./environments";
import { FAILED_ID, stubJobs, TIMED_OUT } from "./jobs";
import { SALES_SQL, stubQueries } from "./queries";
import { signInAsAdmin } from "./session";

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
      const [content, place] = await page
        .locator(".page")
        .evaluate((node) => [
          node.getBoundingClientRect().width,
          node.parentElement?.getBoundingClientRect().width ?? 0,
        ]);
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
