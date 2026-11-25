/**
 * One attempt at a sign-in action (a passkey touch, using a link): its wait in words and what
 * went wrong, announced to screen readers.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ProviderRedirect } from "../api/oidc";
import { needsPasskey, problemText } from "./messages";

/** What {@link useAttempt} hands back. */
export interface Attempt {
  /** True while the action runs. */
  readonly busy: boolean;
  /** What went wrong the last time, in words, or null. */
  readonly error: string | null;
  /** True when it went wrong because the person has no passkey to touch. */
  readonly addPasskey: boolean;
  /**
   * Runs the action once.
   *
   * @param action - The action.
   * @param describe - Names a failure; {@link problemText} by default.
   */
  readonly run: (action: () => Promise<void>, describe?: (error: unknown) => string) => void;
}

/**
 * Keeps the state of one action at a time. An action that sent the browser to the identity
 * provider (`ProviderRedirect`) ends without a failure and stays busy, as the page is being
 * left; a return to it from the browser's history (`pageshow` from the back-forward cache)
 * clears that.
 *
 * @returns The attempt.
 */
export function useAttempt(): Attempt {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addPasskey, setAddPasskey] = useState(false);
  // Back from the provider through the browser's history, the page is shown as it was left:
  // nothing is under way any more.
  useEffect(() => {
    const shown = (event: PageTransitionEvent): void => {
      if (event.persisted) {
        setBusy(false);
      }
    };
    window.addEventListener("pageshow", shown);
    return () => {
      window.removeEventListener("pageshow", shown);
    };
  }, []);
  const run = useCallback(
    (action: () => Promise<void>, describe?: (error: unknown) => string) => {
      setBusy(true);
      setError(null);
      setAddPasskey(false);
      action().then(
        () => {
          setBusy(false);
        },
        (failure: unknown) => {
          if (failure instanceof ProviderRedirect) {
            // The page is being left: the action stays busy, so a second press starts nothing.
            return;
          }
          setBusy(false);
          setError(describe === undefined ? problemText(t, failure) : describe(failure));
          setAddPasskey(needsPasskey(failure));
        },
      );
    },
    [t],
  );
  return { busy, error, addPasskey, run };
}

/** Properties of {@link AttemptStatus}. */
export interface AttemptStatusProps {
  /** The attempt. */
  readonly attempt: Attempt;
  /** What the person waits for, in words. */
  readonly waiting: string;
}

/**
 * The attempt's wait in a status region and its failure as an alert; when the person has no
 * passkey to touch, the alert links to the page where they add one.
 *
 * @param props - The attempt and the words for its wait.
 * @param props.attempt - The attempt.
 * @param props.waiting - What the person waits for.
 * @returns The two regions.
 */
export function AttemptStatus({ attempt, waiting }: AttemptStatusProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <p className="step-status" role="status">
        {attempt.busy ? waiting : ""}
      </p>
      {attempt.error !== null && (
        <p className="step-error" role="alert">
          {attempt.error}
          {attempt.addPasskey && (
            <>
              {" "}
              <Link to="/account">{t("passkey.add")}</Link>
            </>
          )}
        </p>
      )}
    </>
  );
}
