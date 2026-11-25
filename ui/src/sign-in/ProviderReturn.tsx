/**
 * The two pages the identity provider sends the browser back to: `/sign-in/oidc`, which
 * finishes a sign-in (an invite or a recovery link too), and `/sign-in/oidc/confirmed`, which
 * finishes a link from the profile or a provider step-up in the session that started it.
 *
 * @remarks
 * Loaded on demand, with their strings. Each finishes its flow once per page load: a flow works
 * once, so a second render (the interface makes a new router once the person's rights are known)
 * reads the first answer again instead of finishing twice.
 * @packageDocumentation
 */
import { type JSX, useEffect, useEffectEvent, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Navigate, useNavigate } from "react-router";

import { getProvider } from "../api/oidc";
import {
  finishInSession,
  finishProviderSignIn,
  type ProviderConfirmedOut,
  takeProviderTrip,
} from "../api/providerSession";
import type { ProviderTrip } from "../api/oidc";
import { forgetKeptCode } from "./keptCode";
import { isLocalPath } from "./localPath";
import { refusalText } from "./refusals";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";
import type { ConfirmedNote } from "./ProviderNote";

/** How finishing a flow ended. */
type Outcome<T> =
  { readonly ok: true; readonly value: T } | { readonly ok: false; readonly error: unknown };

/**
 * Runs a finish once per page load: a call while one is under way or done gets its answer.
 */
class Once<T> {
  readonly #run: () => Promise<T>;
  #answer: Promise<Outcome<T>> | null = null;

  /**
   * Builds it.
   *
   * @param run - The finish.
   */
  constructor(run: () => Promise<T>) {
    this.#run = run;
  }

  /**
   * Finishes, or reads the finish under way or done.
   *
   * @returns How it ended.
   */
  get(): Promise<Outcome<T>> {
    this.#answer ??= this.#run().then(
      (value): Outcome<T> => ({ ok: true, value }),
      (error: unknown): Outcome<T> => ({ ok: false, error }),
    );
    return this.#answer;
  }

  /** Forgets the answer, once it has been acted on. */
  forget(): void {
    this.#answer = null;
  }
}

const signIn = new Once(async () => (await finishProviderSignIn()).return_to);

/** What the confirmation finish found. */
interface Confirmed {
  /** The server's answer. */
  readonly answer: ProviderConfirmedOut;
  /** The note for the page it opens. */
  readonly note: ConfirmedNote;
}

/** The in-session flow this tab started, read once per page load. */
let trip: ProviderTrip | null | undefined;

/**
 * The in-session flow this tab started.
 *
 * @returns The flow, or null.
 */
function startedTrip(): ProviderTrip | null {
  trip ??= takeProviderTrip();
  return trip;
}

const confirmation = new Once(async (): Promise<Confirmed> => {
  const started = startedTrip();
  const [answer, info] = await Promise.all([finishInSession(), getProvider().catch(() => null)]);
  return {
    answer,
    note: {
      purpose: started?.purpose ?? "step_up",
      provider: info?.display_name ?? null,
    },
  };
});

/**
 * Keeps a page only when it is a local path.
 *
 * @param path - The path the server or the tab named.
 * @returns The path, or Home.
 */
function local(path: string | undefined): string {
  return isLocalPath(path) ? path : "/";
}

/** Properties of {@link Refused}. */
interface RefusedProps {
  /** The heading. */
  readonly title: string;
  /** What was thrown. */
  readonly error: unknown;
  /** Starts again. */
  readonly onAgain: () => void;
}

/**
 * A refusal: its sentence with the next step, and Try again as the one action. The step's
 * heading takes the focus.
 *
 * @param props - The heading, the refusal and the action.
 * @param props.title - The heading.
 * @param props.error - What was thrown.
 * @param props.onAgain - Starts again.
 * @returns The page.
 */
function Refused({ title, error, onAgain }: RefusedProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Step title={title}>
      <p>{refusalText(t, error)}</p>
      <div className="step-actions">
        <Button className="button button-primary" onPress={onAgain}>
          {t("oidc.refused.tryAgain")}
        </Button>
      </div>
    </Step>
  );
}

/**
 * Says the page is waiting for the server, in words.
 *
 * @param props - The words.
 * @param props.title - The heading.
 * @param props.status - What it waits for.
 * @returns The page.
 */
function Waiting({ title, status }: { readonly title: string; readonly status: string }) {
  return (
    <Step title={title}>
      <p className="step-status" role="status">
        {status}
      </p>
    </Step>
  );
}

/**
 * Leaves for the page the person was going to, and forgets the answer it acted on.
 *
 * @param props - Where to go.
 * @param props.to - The page.
 * @param props.done - Forgets the answer.
 * @returns The redirect.
 */
function Arrive({ to, done }: { readonly to: string; readonly done: () => void }): JSX.Element {
  useEffect(done, [done]);
  return <Navigate to={to} replace />;
}

/**
 * `/sign-in/oidc`: finishes the sign-in through the provider and opens the page the person was
 * going to, or names the refusal with Try again (back to `/sign-in`).
 *
 * @returns The page.
 */
export function ProviderReturn(): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const navigate = useNavigate();
  const [outcome, setOutcome] = useState<Outcome<string> | null>(null);
  const finished = useEffectEvent((ended: Outcome<string>) => {
    setOutcome(ended);
    if (ended.ok) {
      void session.refresh();
    } else {
      signIn.forget();
    }
  });
  useEffect(() => {
    let live = true;
    void signIn.get().then((ended) => {
      if (live) {
        finished(ended);
      }
    });
    return () => {
      live = false;
    };
  }, []);
  if (outcome !== null && !outcome.ok) {
    return (
      <Refused
        title={t("oidc.refused.title")}
        error={outcome.error}
        onAgain={() => {
          void navigate("/sign-in", { replace: true });
        }}
      />
    );
  }
  if (outcome !== null && session.state === "signed-in") {
    return <Arrive to={local(outcome.value)} done={forgetSignIn} />;
  }
  return <Waiting title={t("oidc.finishing.title")} status={t("oidc.finishing.status")} />;
}

/** Forgets the sign-in's answer once the page it named is open. */
function forgetSignIn(): void {
  signIn.forget();
}

/**
 * `/sign-in/oidc/confirmed`: finishes a link or a provider step-up and goes back to the page it
 * was started from, with the note that says what happened; a refusal is named with Try again,
 * which goes back there too. The interrupted action is never sent again: the person presses it.
 *
 * @returns The page.
 */
export function ProviderConfirmed(): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [refused, setRefused] = useState<{ readonly error: unknown } | null>(null);
  const ended = useEffectEvent((outcome: Outcome<Confirmed>) => {
    confirmation.forget();
    trip = undefined;
    if (outcome.ok) {
      const note: ConfirmedNote = outcome.value.note;
      void navigate(local(outcome.value.answer.return_to), {
        replace: true,
        state: { providerConfirmed: note },
      });
    } else {
      // A device code kept for the return waits for nothing now.
      forgetKeptCode();
      setRefused({ error: outcome.error });
    }
  });
  const [back] = useState(() => local(startedTrip()?.back));
  useEffect(() => {
    let live = true;
    void confirmation.get().then((outcome) => {
      if (live) {
        ended(outcome);
      }
    });
    return () => {
      live = false;
    };
  }, []);
  if (refused !== null) {
    return (
      <Refused
        title={t("oidc.refused.confirmTitle")}
        error={refused.error}
        onAgain={() => {
          void navigate(back, { replace: true });
        }}
      />
    );
  }
  return (
    <Waiting title={t("oidc.finishing.confirmTitle")} status={t("oidc.finishing.confirmStatus")} />
  );
}
