/**
 * The note on the page a confirmation at the identity provider came back to: confirmed, do it
 * again now; or linked, you now sign in there.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef } from "react";
import { Trans, useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import { VisibleText } from "../shell/VisibleText";

/** What `/sign-in/oidc/confirmed` hands the page it opens, in the history state. */
export interface ConfirmedNote {
  /** What was confirmed: a provider step-up, or a link from the profile. */
  readonly purpose: "link" | "step_up";
  /** The provider's name, or null when it could not be read. */
  readonly provider: string | null;
}

/**
 * The note in the history state, when it is one.
 *
 * @param state - The location's state.
 * @returns The note, or null.
 */
function noteIn(state: unknown): ConfirmedNote | null {
  const note = (state as { readonly providerConfirmed?: Partial<ConfirmedNote> } | null)
    ?.providerConfirmed;
  if (note === undefined || (note.purpose !== "link" && note.purpose !== "step_up")) {
    return null;
  }
  return {
    purpose: note.purpose,
    provider: typeof note.provider === "string" ? note.provider : null,
  };
}

/**
 * Renders the note above the page and moves the focus to it, after the shell (or a sign-in
 * step) has moved it to the page's heading. The interrupted action is not sent again: the note says to press it.
 *
 * @returns The note, or nothing.
 */
export function ProviderNote(): JSX.Element | null {
  const { t } = useTranslation();
  const location = useLocation();
  const note = noteIn(location.state);
  const ref = useRef<HTMLParagraphElement>(null);
  const key = note === null ? null : location.key;
  useEffect(() => {
    if (key === null) {
      return;
    }
    let live = true;
    // After every effect of this commit: the shell's or a sign-in step's own move to its
    // heading comes first, and the note takes the focus from it.
    queueMicrotask(() => {
      if (live) {
        ref.current?.focus();
      }
    });
    return () => {
      live = false;
    };
  }, [key]);
  if (note === null) {
    return null;
  }
  const name = <VisibleText text={note.provider ?? t("notices.confirmed.provider")} />;
  return (
    <p className="page-notice" role="status" tabIndex={-1} ref={ref}>
      <Trans t={t} i18nKey={`notices.confirmed.${note.purpose}`} components={{ name }} />
    </p>
  );
}
