import { expect, type Page, test } from "@playwright/test";

import { runAxe } from "./axe";
import { signInAsAdmin, STUBBED, violations } from "./session";

const KEY = "lakana.preferences";

// The area, its address and the heading it opens on: Settings opens on its first place, Status.
const AREAS = [
  ["Home", "/", "Home"],
  ["Catalog", "/catalog", "Catalog"],
  ["Queries", "/queries", "Queries"],
  ["Jobs", "/jobs", "Jobs"],
  ["Connections", "/connections", "Connections"],
  ["Settings", "/settings", "Status"],
] as const;

/**
 * Collects the console errors and page errors of a page.
 *
 * @param page - The page to listen to.
 * @returns The list, filled as errors arrive.
 */
function consoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("every area has no axe violations", async ({ page }) => {
      for (const [, path, heading] of AREAS) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
        // An administrator has access: Home never tells them to ask for it.
        await expect(page.getByText(/no access yet/)).toHaveCount(0);
        expect(await violations(page), path).toEqual([]);
      }
      await page.getByRole("button", { name: "Signed in as admin" }).click();
      await expect(page.getByRole("menu", { name: "Preferences and account" })).toBeVisible();
      expect(await violations(page), "user menu").toEqual([]);
      await page.getByRole("menuitem", { name: "Profile picture…" }).click();
      await expect(page.getByRole("dialog", { name: "Profile picture" })).toBeVisible();
      expect(await violations(page), "profile picture").toEqual([]);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Search" }).click();
      await expect(page.getByRole("dialog", { name: "Search" })).toBeVisible();
      expect(await violations(page), "palette").toEqual([]);
    });

    test("a deep link loads styled", async ({ page }) => {
      await page.goto("/catalog/prod/gold.sales/fact_sales");

      const heading = page.getByRole("heading", { level: 1, name: "fact_sales" });
      await expect(heading).toBeVisible();
      const font = await heading.evaluate((node) => getComputedStyle(node).fontFamily);
      expect(font).toMatch(/^"?IBM Plex Sans/);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    });

    test("the shell asks the API only for the stubbed session routes", async ({ page }) => {
      const requests: string[] = [];
      page.on("request", (request) => requests.push(request.url()));

      await page.goto("/");
      for (const [name, , heading] of AREAS) {
        await page.getByRole("link", { name, exact: true }).click();
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      }
      await page.waitForLoadState("networkidle");

      const api = requests
        .filter((url) => url.includes("/api/") || url.includes("/v1/"))
        .map((url) => new URL(url).pathname);
      expect(api.length).toBeGreaterThan(0);
      expect(api.filter((path) => !STUBBED.includes(path))).toEqual([]);
    });

    test("no console error on any area", async ({ page }) => {
      // A violation of the content security policy shows up here.
      const errors = consoleErrors(page);

      for (const [, path, heading] of AREAS) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      }
      // The picture is read with the CSRF header and shown from a blob: URL, which the policy
      // allows; a blocked one would not load and would show up as an error.
      const picture = page.getByRole("button", { name: "Signed in as admin" }).locator("img");
      await expect(picture).toHaveAttribute("src", /^blob:/);
      await expect
        .poll(() => picture.evaluate((image: HTMLImageElement) => image.naturalWidth))
        .toBeGreaterThan(0);
      // The floating surfaces: React Aria inserts its own styles for them.
      await page.getByRole("button", { name: "Signed in as admin" }).click();
      await expect(page.getByRole("menu", { name: "Preferences and account" })).toBeVisible();
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Search" }).click();
      await expect(page.getByRole("dialog", { name: "Search" })).toBeVisible();
      await page.keyboard.press("Escape");
      await page.waitForLoadState("networkidle");

      expect(errors).toEqual([]);
    });

    test("Ctrl+K and Meta+K open the palette; Escape closes it", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
      const dialog = page.getByRole("dialog", { name: "Search" });

      for (const chord of ["Control+k", "Meta+k"]) {
        await page.keyboard.press(chord);
        await expect(dialog).toBeVisible();
        await expect(page.getByRole("searchbox")).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
      }

      await page.keyboard.press("Control+k");
      await page.keyboard.type("jobs");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/jobs$/);
      await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeFocused();
    });

    test("the theme survives a reload without a flash", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
      const other = theme === "dark" ? "Light" : "Dark";
      await page.getByRole("button", { name: "Signed in as admin" }).click();
      // One density: the menu offers none.
      await expect(page.getByRole("menuitemradio", { name: "Compact", exact: true })).toHaveCount(
        0,
      );
      await page.getByRole("menuitemradio", { name: other, exact: true }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", other.toLowerCase());

      // Without the interface's own script, only theme-boot.js can set the attributes.
      await page.route("**/assets/*.js", (route) => route.abort());
      await page.reload();

      await expect(page.locator("html")).toHaveAttribute("data-theme", other.toLowerCase());
      await expect(page.locator("html")).not.toHaveAttribute("data-density", /.*/);
      const ground = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(ground).toBe(other === "Dark" ? "rgb(15, 23, 28)" : "rgb(232, 238, 241)");
    });

    test("no horizontal scroll at 768 px and at 1280 px", async ({ page }) => {
      for (const width of [768, 1280]) {
        await page.setViewportSize({ width, height: 800 });
        for (const [, path, heading] of AREAS) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          );
          expect(overflow, `${path} at ${String(width)} px`).toBeLessThanOrEqual(0);
        }
      }
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`320 px wide (WCAG 1.4.10 reflow), ${theme}`, () => {
    test.use({ viewport: { width: 320, height: 640 } });

    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await page.addInitScript(
        ([key, value]) => {
          window.localStorage.setItem(key ?? "", value ?? "");
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("no horizontal scroll and no axe violations on every area", async ({ page }) => {
      for (const [, path, heading] of AREAS) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow, path).toBeLessThanOrEqual(0);
        expect(await violations(page), path).toEqual([]);
      }
      await page.getByRole("button", { name: "Menu" }).click();
      await expect(page.getByRole("navigation", { name: "Areas" })).toBeVisible();
      expect(await violations(page), "menu open").toEqual([]);
    });

    test("the area list is reachable by keyboard, in a logical order", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Areas" })).toBeHidden();

      await page.keyboard.press("Tab");
      await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
      await page.keyboard.press("Tab");
      const toggle = page.getByRole("button", { name: "Menu" });
      await expect(toggle).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(toggle).toHaveAttribute("aria-expanded", "true");

      const order: string[] = [];
      for (let stop = 0; stop < 10; stop += 1) {
        await page.keyboard.press("Tab");
        order.push(await page.evaluate(() => document.activeElement?.textContent.trim() ?? ""));
      }
      expect(order).toEqual([
        "SearchCtrl K",
        "Home",
        "Catalog",
        "Queries",
        "Jobs",
        "Connections",
        "Settings",
        "Help",
        // The user menu's text: "Signed in as" is visually hidden before the name.
        "Signed in as admin",
        // Home's first stop: the update note's link.
        "what's new › (opens in a new tab)",
      ]);
      await expect(page.getByRole("link", { name: "Home" })).toHaveAttribute(
        "aria-current",
        "page",
      );

      await page.getByRole("link", { name: "Jobs" }).focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeFocused();
      await expect(page.getByRole("navigation", { name: "Areas" })).toBeHidden();
    });

    test("the user menu and the palette fit", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

      await page.getByRole("button", { name: "Menu" }).click();
      await page.getByRole("button", { name: "Signed in as admin" }).click();
      const menu = page.getByRole("menu", { name: "Preferences and account" });
      await expect(menu).toBeVisible();
      // The surface fits; a menu taller than the room left above its button scrolls inside it
      // (vertical scrolling is allowed at 320 px, WCAG 1.4.10), so its last item is reachable.
      await expect(page.locator(".float")).toBeInViewport({ ratio: 1 });
      const last = menu.getByRole("menuitem", { name: "Sign out everywhere" });
      await last.scrollIntoViewIfNeeded();
      await expect(last).toBeInViewport({ ratio: 1 });
      await page.keyboard.press("Escape");

      await page.keyboard.press("Control+k");
      const dialog = page.getByRole("dialog", { name: "Search" });
      await expect(dialog).toBeVisible();
      await expect(dialog).toBeInViewport({ ratio: 1 });
      expect(await violations(page), "palette").toEqual([]);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}

/**
 * Answers the administrator's interface state as for a first sign-in: the tour not yet seen.
 *
 * @param page - The page, after {@link signInAsAdmin} (a later route wins).
 */
async function firstSignIn(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/v1/principals/admin/interface-state",
    (route) =>
      route.fulfill({
        json: { tour_seen: false, get_started_hidden: true, update_note_seen: null },
      }),
  );
}

/**
 * Whether two boxes overlap.
 *
 * @param a - One box.
 * @param b - The other.
 * @returns True when they share any area.
 */
function overlap(
  a: { x: number; y: number; width: number; height: number } | null,
  b: { x: number; y: number; width: number; height: number } | null,
): boolean {
  if (a === null || b === null) {
    return true;
  }
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test.describe(`Help, the shortcuts and the tour, ${theme}, ${String(width)} px`, () => {
      test.use({ viewport: { width, height: width === 320 ? 640 : 800 } });

      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await firstSignIn(page);
        await page.addInitScript(
          ([key, value]) => {
            window.localStorage.setItem(key ?? "", value ?? "");
          },
          [KEY, JSON.stringify({ theme, language: "en" })],
        );
      });

      test("the welcome and the steps fit, keep focus inside and have no axe violations", async ({
        page,
      }) => {
        await page.goto("/");
        const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
        await expect(welcome).toBeVisible();
        await expect(welcome).toBeInViewport({ ratio: 1 });
        expect(await violations(page), "welcome").toEqual([]);

        await welcome.getByRole("button", { name: "Start the tour" }).click();
        const name = width === 320 ? "In the menu: Home" : "Home";
        const step = page.getByRole("dialog", { name, exact: true });
        await expect(step).toBeVisible();
        await expect(step).toBeInViewport({ ratio: 1 });
        expect(await step.evaluate((node) => node.contains(document.activeElement))).toBe(true);
        expect(await violations(page), "first step").toEqual([]);
        if (width === 1280) {
          // The step sits beside its place, never over it (WCAG 2.4.11).
          const place = page.getByRole("link", { name: "Home", exact: true });
          await expect(place).toHaveClass(/tour-target/);
          expect(overlap(await step.boundingBox(), await place.boundingBox())).toBe(false);
        }
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow).toBeLessThanOrEqual(0);

        // On to the last step.
        for (let index = 0; index < 7; index += 1) {
          await page.getByRole("button", { name: "Next" }).click();
        }
        const last = page.getByRole("dialog", {
          name: width === 320 ? "In the menu: Help" : "Help",
        });
        await expect(last).toBeVisible();
        await expect(
          // The first open step of the stubbed Get started: the repository is connected.
          last.getByRole("link", { name: "Invite your team" }),
        ).toBeVisible();
        expect(await violations(page), "last step").toEqual([]);

        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toHaveCount(0);
        // Begun at the welcome, the tour had no place to return to: focus goes to the heading.
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeFocused();
      });

      test("Help and the shortcuts have no axe violations", async ({ page }) => {
        await page.goto("/");
        await page
          .getByRole("dialog", { name: "Welcome to Lakana" })
          .getByRole("button", { name: "Skip" })
          .click();
        if (width === 320) {
          await page.getByRole("button", { name: "Menu" }).click();
        }

        const help = page.getByRole("button", { name: "Help" });
        await help.click();
        const menu = page.getByRole("menu", { name: "Help" });
        await expect(menu).toBeVisible();
        await expect(page.getByText("Lakana 0.23.0")).toBeVisible();
        await expect(
          menu.getByRole("menuitem", { name: "Show “Get started” again" }),
        ).toBeVisible();
        expect(await violations(page), "Help").toEqual([]);

        await menu.getByRole("menuitem", { name: "Keyboard shortcuts" }).click();
        const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
        await expect(dialog).toBeVisible();
        await expect(dialog).toBeInViewport({ ratio: 1 });
        expect(await violations(page), "shortcuts").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await expect(help).toBeFocused();
      });
    });
  }
}

/**
 * Switches the theme in the user menu and reads the page's transitions as the switch applies.
 *
 * @param page - A page showing the shell in the dark theme.
 * @returns The transition of `<body>` and of the sidebar while the colours change.
 */
async function transitionsDuringSwitch(page: Page): Promise<string[]> {
  // A mutation observer reads the styles in the same task as the change, before any timer.
  await page.evaluate(() => {
    const seen: string[] = [];
    Object.assign(window, { seenTransitions: seen });
    new MutationObserver(() => {
      if (document.documentElement.getAttribute("data-theme") !== "light") return;
      for (const node of [document.body, document.querySelector(".sidebar")]) {
        const style = getComputedStyle(node ?? document.body);
        seen.push(`${style.transitionProperty} ${style.transitionDuration}`);
      }
    }).observe(document.documentElement, { attributeFilter: ["data-theme"] });
  });
  await page.getByRole("button", { name: "Signed in as admin" }).click();
  await page.getByRole("menuitemradio", { name: "Light", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  return page.evaluate(() => (window as unknown as { seenTransitions: string[] }).seenTransitions);
}

test.describe("the theme switch", () => {
  test.beforeEach(async ({ page }) => {
    await signInAsAdmin(page);
    await page.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key ?? "", value ?? "");
      },
      [KEY, JSON.stringify({ theme: "dark", language: "en" })],
    );
  });

  test("fades the colours over 200 ms, and nothing on first paint", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    const atRest = await page.evaluate(() => getComputedStyle(document.body).transitionDuration);
    expect(atRest).toBe("0s");

    const during = await transitionsDuringSwitch(page);

    expect(during.length).toBeGreaterThan(0);
    for (const transition of during) {
      expect(transition).toMatch(/^background-color, color, border-color, fill, stroke 0\.2s/);
    }
    await expect(page.locator("html")).not.toHaveAttribute("data-theme-fade");
  });

  test("does not fade under reduced motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

    const during = await transitionsDuringSwitch(page);

    expect(during.length).toBeGreaterThan(0);
    for (const transition of during) {
      // The fade rule applies, but reduced motion holds its duration at zero.
      expect(transition).toBe("background-color, color, border-color, fill, stroke 0s");
    }
  });
});

/**
 * Reads what moves in the tour itself: the animations of its own elements (the welcome's or
 * the step's overlay and dialog, their content, and the outlined place), every transition of
 * those that lasts longer than nothing, and how far the page has scrolled. A spinner or a
 * pulsing glyph elsewhere on the page is not the tour's and is not read.
 *
 * @param page - A page showing the welcome or a step.
 * @returns Whether the dialog was among them, the animations' names, the lasting transitions
 *   and the scroll offset.
 */
async function tourMotion(page: Page): Promise<{
  dialog: boolean;
  animations: string[];
  transitions: string[];
  scrolled: number;
}> {
  return page.evaluate(() => {
    const nodes = [
      ...document.querySelectorAll(
        ".tour-overlay, .tour-overlay *, .tour-step, .tour-step *, .tour-target",
      ),
    ];
    const transitions = nodes.flatMap((node) => {
      const style = getComputedStyle(node);
      const lasting = style.transitionDuration.split(",").some((part) => part.trim() !== "0s");
      return lasting
        ? [`${node.tagName.toLowerCase()}.${node.className}: ${style.transitionDuration}`]
        : [];
    });
    const animations = nodes.flatMap((node) =>
      node
        .getAnimations()
        .map((animation) =>
          animation instanceof CSSAnimation ? animation.animationName : animation.id,
        ),
    );
    return {
      // The tour's own dialog is among the elements read, so an empty list means something.
      dialog: nodes.some((node) => node.matches('[role="dialog"]')),
      animations,
      transitions,
      scrolled: window.scrollX + window.scrollY,
    };
  });
}

for (const width of [1280, 320]) {
  test(`the tour moves nothing by itself, and nothing at all under reduced motion, at ${String(width)} px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
    await signInAsAdmin(page);
    await firstSignIn(page);

    // Without the preference, the tour starts no animation of its own.
    await page.goto("/");
    const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
    await expect(welcome).toBeVisible();
    expect(await tourMotion(page), "welcome").toMatchObject({ dialog: true, animations: [] });
    await welcome.getByRole("button", { name: "Start the tour" }).press("Enter");
    for (let index = 0; index < 8; index += 1) {
      await expect(page.getByRole("dialog", { name: /./ })).toBeVisible();
      expect(await tourMotion(page), `step ${String(index + 1)}`).toMatchObject({
        dialog: true,
        animations: [],
      });
      if (index < 7) {
        await page.getByRole("button", { name: "Next" }).press("Enter");
      }
    }

    // Under reduced motion, no transition lasts either, the hover fades included.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await expect(welcome).toBeVisible();
    expect(await tourMotion(page), "welcome").toEqual({
      dialog: true,
      animations: [],
      transitions: [],
      scrolled: 0,
    });
    await welcome.getByRole("button", { name: "Start the tour" }).click();
    for (let index = 0; index < 8; index += 1) {
      const step = page.getByRole("dialog", { name: /./ });
      await expect(step).toBeVisible();
      await step.getByRole("link").first().hover();
      expect(await tourMotion(page), `step ${String(index + 1)}`).toEqual({
        dialog: true,
        animations: [],
        transitions: [],
        scrolled: 0,
      });
      if (index < 7) {
        await page.getByRole("button", { name: "Next" }).click();
      }
    }
  });
}

for (const width of [1280, 600]) {
  test(`the palette opens centred over the whole window, in its upper third, at ${String(width)} px`, async ({
    page,
  }) => {
    await signInAsAdmin(page);
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

    await page.keyboard.press("Control+k");
    const dialog = page.getByRole("dialog", { name: "Search" });
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();

    expect(box, "the palette").not.toBeNull();
    const centre = (box?.x ?? 0) + (box?.width ?? 0) / 2;
    expect(Math.abs(centre - width / 2)).toBeLessThanOrEqual(2);
    expect(box?.y ?? 0).toBeLessThan(800 / 3);
    expect(await violations(page), "palette").toEqual([]);
  });
}

test("the narrow top bar keeps its height and the content starts right below it", async ({
  page,
}) => {
  const heights: number[] = [];
  await signInAsAdmin(page);
  for (const height of [500, 900, 1400]) {
    await page.setViewportSize({ width: 600, height });
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    const bar = await page.getByRole("banner").boundingBox();
    const main = await page.getByRole("main").boundingBox();
    expect(bar, "the top bar").not.toBeNull();
    expect(main, "the content").not.toBeNull();
    const barBottom = (bar?.y ?? 0) + (bar?.height ?? 0);
    const gap = Math.abs((main?.y ?? 0) - barBottom);
    expect(gap, `at ${String(height)} px high`).toBeLessThanOrEqual(1);
    heights.push(Math.round(bar?.height ?? 0));
  }
  expect(new Set(heights).size, heights.join(", ")).toBe(1);
});

// The browser's own parts (the scrollbars, a form control's popup) follow `color-scheme`, so it
// must match the theme on the page, including a light choice under a dark system.
for (const [system, theme, expected] of [
  ["dark", "system", "dark"],
  ["light", "system", "light"],
  ["light", "dark", "dark"],
  ["dark", "light", "light"],
] as const) {
  test(`the browser's own parts are ${expected} with the ${theme} theme under a ${system} system`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: system });
    await signInAsAdmin(page);
    await page.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key ?? "", value ?? "");
      },
      [KEY, JSON.stringify({ theme, language: "en" })],
    );
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

    const scheme = await page.evaluate(
      () => getComputedStyle(document.documentElement).colorScheme,
    );

    expect(scheme).toBe(expected);
  });
}

test("axe catches a missing alt text", async ({ page }) => {
  // The positive control: the checks above would pass vacuously if axe found nothing at all.
  await page.setContent('<img src="x.png">');

  const results = await runAxe(page);

  expect(results.violations.map((violation) => violation.id)).toContain("image-alt");
});
