import { render, screen, within } from "@testing-library/react";
import i18next from "i18next";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { JobOut } from "../api/jobs";
import { aJob } from "../test/jobs";
import { JobFacts } from "./JobFacts";

/**
 * Renders the facts of a job in `prod`.
 *
 * @param job - The job.
 * @returns The facts' description list.
 */
function renderFacts(job: JobOut): HTMLElement {
  render(
    <MemoryRouter>
      <JobFacts environment="prod" job={job} />
    </MemoryRouter>,
  );
  return screen.getByRole("region", { name: "Facts" });
}

/**
 * The answer beside a term.
 *
 * @param term - The term's words.
 * @returns The definition.
 */
function fact(term: string): HTMLElement {
  const dt = screen.getByText(term, { selector: "dt" });
  const dd = dt.nextElementSibling;
  if (!(dd instanceof HTMLElement)) {
    throw new Error(`no answer for ${term}`);
  }
  return dd;
}

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

describe("JobFacts", () => {
  it("shows the repository, lakana.yaml at the short commit and the host link only for https", () => {
    const panel = renderFacts(aJob({ repository: "team/\u202Eanalytics" }));

    expect(panel).toHaveClass("flat-panel");
    expect(fact("Repository")).toHaveTextContent(
      "team/U+202Eanalytics · lakana.yaml @ 4f9d2a7 · open in repository ↗",
    );
    expect(fact("Repository")).toHaveTextContent(/ · Deploys of team\/U\+202Eanalytics$/u);
    expect(
      within(fact("Repository")).getByRole("link", { name: /open in repository/ }),
    ).toHaveAttribute("href", "https://github.com/acme/analytics/blob/4f9d2a7/lakana.yaml");
  });

  it.each(["http://git.example.com/analytics/lakana.yaml", "javascript:alert(1)", null])(
    "shows no host link for %s",
    (url) => {
      renderFacts(aJob({ source_url: url }));

      expect(fact("Repository")).toHaveTextContent("analytics · lakana.yaml @ 4f9d2a7");
      expect(screen.queryByRole("link", { name: /open in repository/ })).toBeNull();
    },
  );

  it("links to its repository's deploys", () => {
    renderFacts(aJob({ repository: "team/analytics" }));

    expect(
      within(fact("Repository")).getByRole("link", { name: "Deploys of team/analytics" }),
    ).toHaveAttribute("href", "/jobs/deploys?environment=prod&repository=team%2Fanalytics");
  });

  it("shows the schedule in words with its zone and next run, or the sentence of no schedule", () => {
    renderFacts(aJob());
    expect(fact("Schedule")).toHaveTextContent(
      `daily 02:00 Europe/Berlin · next run ${when("2026-11-18T01:00:00Z")}`,
    );
    expect(fact("After each deploy")).toHaveTextContent("No");
  });

  it("shows a cron it cannot word as it is, in monospace", () => {
    renderFacts(aJob({ cron: "*/15 8-18 * * 1-5", timezone: "UTC", next_run_at: null }));

    const text = within(fact("Schedule")).getByText("*/15 8-18 * * 1-5 UTC");
    expect(text).toHaveClass("jobs-verbatim");
  });

  it("says when a job has no schedule here", () => {
    renderFacts(
      aJob({
        cron: null,
        timezone: null,
        next_run_at: null,
        triggers: [{ kind: "deploy" }, { kind: "on_demand" }],
      }),
    );

    expect(fact("Schedule")).toHaveTextContent(
      "No schedule here; it starts by hand, through the API or after each deploy.",
    );
    expect(fact("After each deploy")).toHaveTextContent("Yes");
  });

  it("shows paused in words", () => {
    renderFacts(aJob({ paused: true, state: "paused", next_run_at: null }));

    expect(fact("Schedule")).toHaveTextContent("daily 02:00 Europe/Berlin · paused");
  });

  it("shows a selection without upstream or downstream", () => {
    renderFacts(aJob({ upstream: false }));

    expect(fact("Runs")).toHaveTextContent(/^select silver\.\*$/);
  });

  it("shows the selection with upstream and downstream", () => {
    renderFacts(aJob({ select: ["silver.*", "gold.\u202Esales"], downstream: true }));

    expect(fact("Runs")).toHaveTextContent(
      "select silver.* gold.U+202Esales +upstream +downstream",
    );
  });

  it("shows where it notifies, with the secret only when the server sent it", () => {
    const { unmount } = render(
      <MemoryRouter>
        <JobFacts
          environment="prod"
          job={aJob({
            notify: {
              format: "slack",
              events: ["failed", "recovered", "columns_changed", "health"],
              secret: "alerts-webhook",
            },
          })}
        />
      </MemoryRouter>,
    );
    expect(fact("Notifies")).toHaveTextContent(
      "Notifies on failure, recovery, column changes and health signals · Slack · via secret alerts-webhook",
    );
    unmount();

    renderFacts(aJob({ notify: { format: "teams", events: ["failed", "later_event"] } }));
    expect(fact("Notifies")).toHaveTextContent("Notifies on failure and later_event · Teams");
    expect(fact("Notifies")).not.toHaveTextContent("secret");
  });

  it("words a single event alone", () => {
    renderFacts(aJob({ notify: { format: "json", events: ["failed"] } }));
    expect(fact("Notifies")).toHaveTextContent("Notifies on failure · JSON");
  });

  it("says how to add a notification to a job without one", () => {
    renderFacts(aJob({ notify: null }));

    expect(fact("Notifies")).toHaveTextContent(
      "No notification. Add notify: to the job in lakana.yaml.",
    );
  });

  it("shows why a start waits with a link to the run it waits for", () => {
    renderFacts(
      aJob({
        state: "waiting",
        waiting_for: { code: "job_waits", message: "A run of nightly_\u202Esales is running." },
        waits_for_run_id: "r1",
      }),
    );

    expect(document.querySelector(".jobs-waiting")).toHaveTextContent(
      "A run of nightly_U+202Esales is running. Open the run it waits for",
    );
    expect(screen.getByRole("link", { name: "Open the run it waits for" })).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r1",
    );
  });
});
