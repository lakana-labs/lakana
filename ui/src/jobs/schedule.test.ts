import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { describeSchedule } from "./schedule";

describe("describeSchedule", () => {
  it.each([
    ["* * * * *", "every minute"],
    ["5 * * * *", "hourly at :05"],
    ["0 2 * * *", "daily 02:00"],
    ["30 6 * * 1", "weekly on Monday 06:30"],
    ["0 3 1 * *", "monthly on day 1 03:00"],
  ])("words %s", (cron, text) => {
    expect(describeSchedule(cron, "Europe/Berlin", i18next.t.bind(i18next))).toEqual({
      text: `${text} Europe/Berlin`,
      verbatim: false,
    });
  });

  it.each(["*/15 8-18 * * 1-5", "0 2 * * 7", "61 2 * * *", "0 25 * * *", "0 0 32 * *", "a b"])(
    "shows %s as it is",
    (cron) => {
      expect(describeSchedule(cron, "UTC", i18next.t.bind(i18next))).toEqual({
        text: `${cron} UTC`,
        verbatim: true,
      });
    },
  );
});
