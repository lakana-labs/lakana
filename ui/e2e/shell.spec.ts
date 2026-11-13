import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { signInAsAdmin, STUBBED, violations } from "./session";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const LIVE_ANNOUNCER = "[data-live-announcer]";
const KEY = "lakana.preferences";

const AREAS = [
  ["Home", "/"],
  ["Catalog", "/catalog"],
  ["Queries", "/queries"],
  ["Jobs", "/jobs"],
  ["Connections", "/connections"],
  ["Access", "/access"],
  ["Settings", "/settings"],
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
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("every area has no axe violations", async ({ page }) => {
        for (const [name, path] of AREAS) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
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
        await page.goto("/catalog/does/not/exist");

        const heading = page.getByRole("heading", { level: 1, name: "This page does not exist" });
        await expect(heading).toBeVisible();
        const font = await heading.evaluate((node) => getComputedStyle(node).fontFamily);
        expect(font).toMatch(/^"?IBM Plex Sans/);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      });

      test("the shell asks the API only for the stubbed session routes", async ({ page }) => {
        const requests: string[] = [];
        page.on("request", (request) => requests.push(request.url()));

        await page.goto("/");
        for (const [name] of AREAS) {
          await page.getByRole("link", { name, exact: true }).click();
          await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
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

        for (const [name, path] of AREAS) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
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
        // The check mark is not part of the name of the selected choice.
        const chosen = density === "roomy" ? "Roomy" : "Compact";
        await expect(page.getByRole("menuitemradio", { name: chosen, exact: true })).toBeChecked();
        await page.getByRole("menuitemradio", { name: other, exact: true }).click();
        await expect(page.locator("html")).toHaveAttribute("data-theme", other.toLowerCase());

        // Without the interface's own script, only theme-boot.js can set the attributes.
        await page.route("**/assets/*.js", (route) => route.abort());
        await page.reload();

        await expect(page.locator("html")).toHaveAttribute("data-theme", other.toLowerCase());
        await expect(page.locator("html")).toHaveAttribute("data-density", density);
        const ground = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
        expect(ground).toBe(other === "Dark" ? "rgb(15, 23, 28)" : "rgb(232, 238, 241)");
      });

      test("no horizontal scroll at 768 px and at 1280 px", async ({ page }) => {
        for (const width of [768, 1280]) {
          await page.setViewportSize({ width, height: 800 });
          for (const [name, path] of AREAS) {
            await page.goto(path);
            await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
            const overflow = await page.evaluate(
              () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
            );
            expect(overflow, `${path} at ${String(width)} px`).toBeLessThanOrEqual(0);
          }
        }
      });
    });
  }
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
        [KEY, JSON.stringify({ theme, density: "roomy", language: "en" })],
      );
    });

    test("no horizontal scroll and no axe violations on every area", async ({ page }) => {
      for (const [name, path] of AREAS) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
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
      for (let stop = 0; stop < 11; stop += 1) {
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
        "Access",
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
          [KEY, JSON.stringify({ theme, density: "roomy", language: "en" })],
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
        for (let index = 0; index < 8; index += 1) {
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
      [KEY, JSON.stringify({ theme: "dark", density: "roomy", language: "en" })],
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

test("axe catches a missing alt text", async ({ page }) => {
  // The positive control: the checks above would pass vacuously if axe found nothing at all.
  await page.setContent('<img src="x.png">');

  const results = await new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();

  expect(results.violations.map((violation) => violation.id)).toContain("image-alt");
});
