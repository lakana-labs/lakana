import { afterEach, describe, expect, it, vi } from "vitest";

import { aJob } from "../test/jobs";
import { fakeServer } from "../test/server";
import { JOBS_READS_IN_FLIGHT, readJobsOf } from "./readJobsOf";

const jobsOf = (environment: string): string => `GET /api/v1/environments/${environment}/jobs`;

afterEach(() => {
  vi.useRealTimers();
});

describe("readJobsOf", () => {
  it("answers each environment in order, a failure as a failure", async () => {
    fakeServer({
      [jobsOf("dev")]: { status: 200, body: [aJob({ environment: "dev" })] },
      [jobsOf("prod")]: { status: 403, body: { detail: "You may not read prod." } },
    });

    const answers = await readJobsOf(["dev", "prod"]);

    expect(answers).toHaveLength(2);
    expect(answers[0]).toEqual({ jobs: [aJob({ environment: "dev" })] });
    expect(answers[1]).toHaveProperty("failure");
  });

  it("reads at most four environments at once", async () => {
    let open = 0;
    let most = 0;
    const slow = async () => {
      open += 1;
      most = Math.max(most, open);
      await new Promise((resolve) => setTimeout(resolve, 5));
      open -= 1;
      return { status: 200, body: [] };
    };
    const names = ["a", "b", "c", "d", "e", "f"];
    fakeServer(Object.fromEntries(names.map((name) => [jobsOf(name), slow])));

    await readJobsOf(names);

    expect(JOBS_READS_IN_FLIGHT).toBe(4);
    expect(most).toBe(4);
  });

  it("gives up on an environment that answers too late, as on a failed one", async () => {
    vi.useFakeTimers();
    fakeServer({
      [jobsOf("dev")]: { status: 200, body: [] },
      [jobsOf("slow")]: () => new Promise(() => undefined),
    });

    const answers = readJobsOf(["dev", "slow"], 5_000);
    await vi.advanceTimersByTimeAsync(5_000);

    const [dev, slow] = await answers;
    expect(dev).toEqual({ jobs: [] });
    expect(slow).toHaveProperty("failure");
  });
});
