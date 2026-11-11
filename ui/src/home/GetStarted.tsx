/**
 * Home's Get started: what to do next, as far as the person may, each step one sentence and
 * one way straight there, ticked off from the server's state.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { GetStartedOut, GetStartedStepId } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { useInterfaceState } from "../shell/InterfaceState";
import { useAttempt } from "../sign-in/Attempt";
import { StatusGlyph } from "../status/StatusGlyph";
import { STEP_WAYS } from "./ways";

/** Properties of {@link StepWay}. */
interface StepWayProps {
  /** The step. */
  readonly id: GetStartedStepId;
}

/**
 * The step's link: inside the interface, or to the documentation site in a new tab, with the
 * command to copy when the step is one.
 *
 * @param props - The step.
 * @param props.id - The step.
 * @returns The link, and the command.
 */
function StepWay({ id }: StepWayProps): JSX.Element {
  const { t } = useTranslation();
  const way = STEP_WAYS[id];
  const label = t(`home.getStarted.steps.${id}.action`);
  if ("to" in way) {
    return <Link to={way.to}>{label}</Link>;
  }
  return (
    <>
      <a href={way.href} target="_blank" rel="noreferrer">
        {label}
        <span className="visually-hidden"> {t("home.newTab")}</span>
      </a>
      {way.command !== undefined && (
        <CopyLine text={way.command} copyLabel={t("comingSoon.copy")} />
      )}
    </>
  );
}

/** Properties of {@link GetStarted}. */
export interface GetStartedProps {
  /** The list, as the server answered it. */
  readonly list: GetStartedOut;
  /** Called once Hide worked, so focus does not fall to the page's body with the button. */
  readonly onGone: () => void;
}

/**
 * Shows the steps while one is open and the person has not hidden the list; "Hide" hides it for
 * good (Help brings it back). A done step shows the succeeded glyph and "Done".
 *
 * @param props - The list.
 * @param props.list - The list, as the server answered it.
 * @param props.onGone - Called once Hide worked.
 * @returns The section, or nothing once complete or hidden.
 */
export function GetStarted({ list, onGone }: GetStartedProps): JSX.Element | null {
  const { t } = useTranslation();
  const heading = useId();
  const { state, change } = useInterfaceState();
  const attempt = useAttempt();
  // The interface state follows Hide (and Help's "Show again") at once; the list's own flag is
  // what the server knew when Home opened.
  const hidden = state?.get_started_hidden ?? list.hidden;
  if (hidden || list.complete || list.steps.length === 0) {
    return null;
  }
  return (
    <section className="home-section" aria-labelledby={heading}>
      <div className="home-section-head">
        <h2 id={heading}>{t("home.getStarted.title")}</h2>
        <Button
          className="button"
          isDisabled={attempt.busy}
          onPress={() => {
            attempt.run(async () => {
              await change({ get_started_hidden: true });
              onGone();
            });
          }}
        >
          {t("home.getStarted.hide")}
        </Button>
      </div>
      {attempt.error !== null && (
        <p className="step-error" role="alert">
          {attempt.error}
        </p>
      )}
      <ul className="home-steps">
        {list.steps.map((step) => (
          <li key={step.id} className={step.done ? "home-step-done" : undefined}>
            <p>{t(`home.getStarted.steps.${step.id}.text`)}</p>
            {step.done ? (
              <span className="status-label">
                <StatusGlyph kind="succeeded" />
                <span>{t("home.getStarted.done")}</span>
              </span>
            ) : (
              <StepWay id={step.id} />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
