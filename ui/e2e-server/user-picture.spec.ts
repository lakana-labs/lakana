import { expect, test } from "@playwright/test";

import { localLink } from "./helpers";

const ORIGIN = "http://localhost:4174";

/** One pixel, a PNG: the server cuts and re-encodes it into a 256 x 256 WebP. */
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
  "base64",
);

test("a profile picture: set, shown from a blob: URL under the policy, refused, removed", async ({
  browser,
  request,
}) => {
  const page = await (await browser.newContext({ baseURL: ORIGIN })).newPage();
  await page.goto(await localLink(request));
  const landed = page.getByRole("heading", { level: 1, name: /^(Add a passkey|Home)$/ });
  await expect(landed).toBeVisible();
  if ((await landed.textContent()) === "Add a passkey") {
    await page.getByRole("button", { name: "Not now" }).click();
  }
  // Runs after sign-in.spec.ts, whose first test expects the welcome: in Chromium it was seen
  // there; elsewhere the welcome opens now.
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  if (
    await welcome.waitFor({ timeout: 5000 }).then(
      () => true,
      () => false,
    )
  ) {
    await welcome.getByRole("button", { name: "Skip" }).click();
    await expect(welcome).toBeHidden();
  }
  // A violation of the content security policy is a console error; listened
  // to once signed in (the landing reads the session before the link signs in: a 401).
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  const trigger = page.getByRole("button", { name: /^Signed in as / });
  await expect(trigger.locator(".avatar")).toHaveText(/^\p{Lu}+$/u);

  await trigger.click();
  await page.getByRole("menuitem", { name: "Profile picture…" }).click();
  const dialog = page.getByRole("dialog", { name: "Profile picture" });
  const field = dialog.getByLabel("Choose a picture");

  // A file the server cannot read: its own sentence.
  await field.setInputFiles({
    name: "broken.png",
    mimeType: "image/png",
    buffer: Buffer.from("x"),
  });
  await expect(dialog.getByRole("alert")).toContainText("PNG");

  await field.setInputFiles({ name: "kim.png", mimeType: "image/png", buffer: PIXEL });
  await expect(dialog.getByText("Your picture is saved.")).toBeVisible();
  const picture = trigger.locator("img");
  await expect(picture).toHaveAttribute("src", /^blob:/);
  await expect
    .poll(() => picture.evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBe(256);

  await dialog.getByRole("button", { name: "Remove picture" }).click();
  await expect(dialog.getByText("Your picture is removed.")).toBeVisible();
  await expect(picture).toHaveCount(0);
  await dialog.getByRole("button", { name: "Done" }).click();

  // The refused upload's 422 is the one console line a browser writes for a failed request.
  expect(errors.filter((error) => !error.includes("422"))).toEqual([]);
  await page.context().close();
});
